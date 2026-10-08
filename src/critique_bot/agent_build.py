"""Builds: which commands are builds, what their output means, and what to do when they fail.

A build (Gradle, Maven, AOSP ``m``, CMake, npm) gets a long timeout, a short
summary at the top of its result (Gradle's "What went wrong", compiler
errors, BUILD SUCCESSFUL/FAILED), hints for the failures crit can recognize
(no SDK, wrong JDK, a corporate proxy's certificate, locked files), one
automatic retry for a transient failure, and the APK/AAB files it produced.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BUILD_TIMEOUT = 1800.0
MAX_BUILD_TIMEOUT = 7200.0
SUMMARY_LINES = 30

_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_WRAPPERS = {"env", "time", "nohup", "command", "exec", "call", "&", ".", "cmd", "/c", "sudo"}

#: Programs whose run is a build, whatever the arguments.
_BUILD_PROGRAMS = {
    "gradlew", "gradle", "mvn", "mvnw", "make", "gmake", "ninja", "m", "mm", "mmm", "mma", "mmma",
    "bazel", "bazelisk", "atest", "msbuild", "xcodebuild", "sbt", "ant", "ndk-build", "flutter",
}
#: Programs that are a build only with one of these first words.
_BUILD_SUBCOMMANDS = {
    "npm": {"install", "ci", "run", "test", "i"},
    "pnpm": {"install", "i", "run", "build", "test"},
    "yarn": {"install", "build", "test", "run"},
    "dotnet": {"build", "test", "publish", "restore"},
    "cargo": {"build", "test", "run", "check"},
    "cmake": {"--build"},
    "go": {"build", "test"},
    "repo": {"sync"},
    "pip": {"install"},
    "python": {"-m"},
}
_BUILD_SCRIPTS = re.compile(r"(?:^|[\\/])build(?:[-_]\w+)?\.(?:sh|bat|cmd|ps1)$", re.I)


def _program(token: str) -> str:
    base = re.split(r"[\\/]", token.strip("\"'").lstrip("&").lower())[-1]
    for suffix in (".exe", ".bat", ".cmd", ".sh", ".ps1"):
        if base.endswith(suffix) and len(base) > len(suffix):
            return base[: -len(suffix)]
    return base


def _segments(command: str) -> list[list[str]]:
    out: list[list[str]] = []
    for segment in _SEGMENT_SPLIT.split(command or ""):
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        tokens = [token for token in tokens if token.strip("\"'")]
        while tokens and (tokens[0].lower() in _WRAPPERS or re.match(r"^[A-Za-z_]\w*=", tokens[0]) or tokens[0].startswith("$env:")):
            if tokens[0].startswith("$env:"):
                break
            tokens = tokens[1:]
        if tokens:
            out.append(tokens)
    return out


def is_build_command(command: str) -> bool:
    """True for a command that builds or installs dependencies, and so needs a long timeout."""
    for tokens in _segments(command):
        if tokens[0].startswith("$env:"):
            continue
        program = _program(tokens[0])
        if program in _BUILD_PROGRAMS or _BUILD_SCRIPTS.search(tokens[0].strip("\"'")):
            return True
        words = {token.lower().strip("\"'") for token in tokens[1:3]}
        if program in _BUILD_SUBCOMMANDS and words & _BUILD_SUBCOMMANDS[program]:
            if program == "python" and not any(t.lower() in {"pip", "build", "pytest"} for t in tokens[1:4]):
                continue
            return True
    return False


def is_gradle_command(command: str) -> bool:
    return any(_program(tokens[0]) in {"gradlew", "gradle"} for tokens in _segments(command))


def gradle_wrapper(folder: Path, *, windows: bool) -> str:
    """The wrapper call for ``folder`` (``.\\gradlew.bat`` or ``./gradlew``), or "" when there is none."""
    if windows and (folder / "gradlew.bat").is_file():
        return ".\\gradlew.bat"
    if (folder / "gradlew").is_file():
        return "./gradlew"
    return ""


# --------------------------------------------------------------------------- output summary

_WHAT_WENT_WRONG = re.compile(r"^\*\s*What went wrong:\s*$", re.M)
_SUMMARY_LINE = re.compile(
    r"^(?:BUILD (?:SUCCESSFUL|FAILED)\b.*"
    r"|FAILURE: .*"
    r"|> Task \S+ FAILED"
    r"|e: .+"
    r"|.+\.(?:java|kt|kts|xml|c|cc|cpp|h|hpp|aidl|gradle|toml):\d+(?::\d+)?:?\s*(?:error|fatal error)\b.*"
    r"|.*\bAAPT: error\b.*"
    r"|ERROR: .+"
    r"|\[ERROR\] .+"
    r"|error: .+"
    r"|FAILED: .+"
    r"|ninja: build stopped.*"
    r"|npm ERR! .+"
    r"|Caused by: .+)$",
    re.M,
)


def summarize(output: str, *, max_lines: int = SUMMARY_LINES) -> list[str]:
    """The lines that say how a build went: the result, Gradle's "What went wrong", and errors.

    Empty when the output has none of them (most non-build commands).
    """
    text = output or ""
    lines: list[str] = []
    seen: set[str] = set()

    def add(line: str) -> None:
        line = line.rstrip()
        key = line.strip()
        if key and key not in seen and len(lines) < max_lines:
            seen.add(key)
            lines.append(line[:400])

    for match in _WHAT_WENT_WRONG.finditer(text):
        block = text[match.end():].lstrip("\n").split("\n")
        add("What went wrong:")
        for line in block[:20]:
            if re.match(r"^\*\s*(Try|Exception is|Get more help|Where):", line):
                break
            if line.strip():
                add("  " + line.strip())
    for match in _SUMMARY_LINE.finditer(text):
        add(match.group(0))
    return lines


# --------------------------------------------------------------------------- hints


@dataclass(frozen=True)
class Context:
    """What crit knows about the machine, for the hints."""

    windows: bool = False
    wrapper: str = ""
    android_sdk: str = ""
    jdks: tuple[tuple[int, str], ...] = ()  # (major version, home)
    java_home: str = ""


def _jdk_list(ctx: Context) -> str:
    if not ctx.jdks:
        return "no JDK was found on this machine"
    return "; ".join(f"JDK {version}: {home}" for version, home in ctx.jdks[:6])


def _set_env(ctx: Context, name: str, value: str) -> str:
    if ctx.windows:
        return f"$env:{name} = '{value}'"
    return f"export {name}='{value}'"


def _rules(ctx: Context) -> list[tuple[re.Pattern[str], str]]:
    wrapper = ctx.wrapper or (".\\gradlew.bat" if ctx.windows else "./gradlew")
    first_jdk = next((home for version, home in ctx.jdks if version >= 17), ctx.jdks[0][1] if ctx.jdks else "")
    java_fix = (
        f"Run the build with JAVA_HOME pointing at a fitting JDK in the same command, for example: "
        f"{_set_env(ctx, 'JAVA_HOME', first_jdk or '<jdk folder>')}; {wrapper} assembleDebug. "
        f"Installed: {_jdk_list(ctx)}. AGP 8.x needs JDK 17 or newer; AGP 7.x needs 11."
    )
    sdk = ctx.android_sdk
    if sdk:
        escaped = sdk.replace("\\", "\\\\").replace(":", "\\:")
        sdk_fix = (
            f"The Android SDK is at {sdk}. Write local.properties in the project root with the line "
            f"sdk.dir={escaped} (local.properties is not committed), or set "
            f"{_set_env(ctx, 'ANDROID_HOME', sdk)} in the same command."
        )
    else:
        sdk_fix = (
            "No Android SDK was found (checked ANDROID_HOME, ANDROID_SDK_ROOT, local.properties, "
            + ("%LOCALAPPDATA%\\Android\\Sdk" if ctx.windows else "~/Android/Sdk and ~/Library/Android/sdk")
            + "). Look for it with find_files or list_files before saying it is missing; Android Studio "
            "shows it under Settings > Languages & Frameworks > Android SDK."
        )
    sdkmanager = (
        "& \"$env:ANDROID_HOME\\cmdline-tools\\latest\\bin\\sdkmanager.bat\"" if ctx.windows
        else "\"$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager\""
    )
    raw: list[tuple[str, str]] = [
        (r"SDK location not found|Define a valid SDK location|ANDROID_HOME (?:is not set|environment variable)|sdk\.dir",
         sdk_fix),
        (r"requires Java \d+|Unsupported class file major version|invalid source release|class file has wrong version"
         r"|release version \d+ not supported|No matching toolchains found|Cannot find a Java installation"
         r"|JAVA_HOME is not set|JAVA_HOME is set to an invalid directory|no 'java' command could be found"
         r"|Could not determine java version|Dependency requires at least JVM runtime version",
         java_fix),
        (r"PKIX path building failed|unable to find valid certification path|SSLHandshakeException"
         r"|certificate_unknown|CertPathValidatorException",
         "HTTPS certificate not trusted: a corporate proxy usually re-signs HTTPS. "
         + ("Use the Windows certificate store: add the line systemProp.javax.net.ssl.trustStoreType=Windows-ROOT to "
            "gradle.properties (for dependencies) and run with $env:GRADLE_OPTS = '-Djavax.net.ssl.trustStoreType=Windows-ROOT' "
            "(for the wrapper download), then rerun."
            if ctx.windows else
            "Import the proxy's root certificate into the JDK (keytool -importcert -cacerts) or point "
            "systemProp.javax.net.ssl.trustStore at a store that has it, then rerun.")),
        (r"UnknownHostException|Could not resolve host|No such host is known|Could not GET '|Could not HEAD '"
         r"|Connection refused|Network is unreachable|Connect timed out|Read timed out|Could not resolve all (?:files|dependencies)"
         r"|Could not download",
         "Network problem reaching a repository. If this machine uses a proxy, put "
         "systemProp.https.proxyHost=<host> and systemProp.https.proxyPort=<port> (and the http. pair) in "
         + ("%USERPROFILE%\\.gradle\\gradle.properties" if ctx.windows else "~/.gradle/gradle.properties")
         + (" (see netsh winhttp show proxy or $env:HTTPS_PROXY)" if ctx.windows else " (see $HTTPS_PROXY)")
         + ". When the dependencies were downloaded before, try --offline."),
        (r"Timeout waiting to lock|currently in use by another Gradle instance|used by another process"
         r"|Unable to delete (?:file|directory)|Failed to delete|AccessDeniedException|Could not create service of type",
         f"Files are locked by another Gradle daemon, Android Studio, or antivirus. Run {wrapper} --stop, then rerun."),
        (r"OutOfMemoryError|Java heap space|GC overhead limit|Metaspace|Gradle build daemon disappeared|daemon disappeared",
         "Gradle ran out of memory or its daemon died. Put org.gradle.jvmargs=-Xmx4g -XX:MaxMetaspaceSize=1g -Dfile.encoding=UTF-8 "
         f"in gradle.properties, run {wrapper} --stop, then rerun."),
        (r"licen[cs]es? (?:for|have) .*not (?:been )?accepted|following Android SDK packages as some licen[cs]es",
         "Accept the SDK licenses, then rerun: "
         + ("1..30 | ForEach-Object { 'y' } | " + sdkmanager + " --licenses" if ctx.windows else "yes | " + sdkmanager + " --licenses")),
        (r"Failed to find target with hash string|Failed to find Build Tools revision|package .* not installed|NDK (?:is )?not (?:installed|configured)"
         r"|No version of NDK matched",
         "A missing SDK package: install it with " + sdkmanager + " \"platforms;android-<N>\" \"build-tools;<version>\" "
         "(or \"ndk;<version>\"), using the numbers from the error, then rerun."),
        (r"Could not find or load main class org\.gradle\.wrapper\.GradleWrapperMain|gradle-wrapper\.jar",
         "gradle/wrapper/gradle-wrapper.jar is missing or broken. Restore it with git checkout -- gradle/wrapper, "
         "or regenerate it with a local gradle: gradle wrapper."),
        (r"Minimum supported Gradle version is|requires Gradle \d|Gradle version .* is required",
         "The Android Gradle plugin needs a newer Gradle: change distributionUrl in gradle/wrapper/gradle-wrapper.properties "
         "to the version the error names, then rerun."),
        (r"Plugin \[id: '[^']+'.*\] was not found|Could not find com\.android\.tools\.build",
         "The plugin could not be found: check pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } } "
         "in settings.gradle(.kts), and the network/proxy hint if it is there."),
        (r"Permission denied.*gradlew|gradlew: Permission denied",
         "Make the wrapper executable: chmod +x gradlew, then rerun."),
        (r"is not recognized as (?:the name of a cmdlet|an internal or external command)|CommandNotFoundException",
         "That program is not on PATH in this shell. PowerShell runs a file in the current folder only with a .\\ prefix: "
         f"{wrapper}. Check the folder with Get-ChildItem and the program with Get-Command <name>."
         if ctx.windows else "That program is not on PATH. Check it with command -v <name>."),
        (r"Filename too long|The filename or extension is too long|path too long|MAX_PATH",
         "Windows path length limit: build from a shorter path (for example C:\\src\\app) or run "
         "git config core.longpaths true."),
        (r"Duplicate class .* found in modules",
         "Two dependencies ship the same class: align their versions or exclude one (see the gradle skill)."),
        (r"Execution failed for task '[^']*(?:compile|kapt|ksp)[^']*'",
         "The code does not compile: fix the e:/error: lines above in the source files, then rerun the same build."),
        (r"Execution failed for task '[^']*(?:lint|Lint)[^']*'",
         "Lint failed: fix the issues it lists (the report path is in the output); do not turn lint off unless the task says so."),
    ]
    return [(re.compile(pattern, re.I), text) for pattern, text in raw]


def hints(output: str, ctx: Context | None = None, *, max_hints: int = 3) -> list[str]:
    """What to do about a failed build, for the failures crit recognizes."""
    ctx = ctx or Context()
    found: list[str] = []
    for pattern, text in _rules(ctx):
        if pattern.search(output or "") and text not in found:
            found.append(text)
        if len(found) >= max_hints:
            break
    return found


# --------------------------------------------------------------------------- retries

_LOCKED = re.compile(
    r"Timeout waiting to lock|currently in use by another Gradle instance|used by another process"
    r"|Gradle build daemon disappeared|daemon disappeared unexpectedly|Could not receive a message from the daemon"
    r"|Could not connect to the Gradle daemon|Could not create service of type",
    re.I,
)
_FLAKY_NETWORK = re.compile(
    r"Read timed out|Connect timed out|Connection reset|Remote host terminated the handshake"
    r"|Received status code 50[0234]|status code: 50[0234]|Premature end of Content-Length|SocketTimeoutException"
    r"|The server closed the connection|Broken pipe",
    re.I,
)


def transient_failure(command: str, output: str) -> tuple[str, bool] | None:
    """``(reason, stop_daemons_first)`` when a failed build is worth one more try, else None.

    Locked files and a dead Gradle daemon are retried after ``--stop``; a
    dropped connection or a 5xx from a repository is retried as is. Compile
    errors, a missing SDK, a wrong JDK, or a certificate problem are not
    retried: running again would fail the same way.
    """
    if not is_build_command(command):
        return None
    text = output or ""
    if re.search(r"^e: |error: |FAILURE: Build failed with an exception\.\s*\n\s*\*\s*Where:", text, re.M) and not _LOCKED.search(text):
        return None
    if _LOCKED.search(text):
        return ("Gradle files were locked or its daemon stopped", is_gradle_command(command))
    if _FLAKY_NETWORK.search(text) and not re.search(r"PKIX|UnknownHost|No such host", text, re.I):
        return ("the network dropped while downloading", False)
    return None


# --------------------------------------------------------------------------- artifacts

_ARTIFACT_GLOBS = (
    "build/outputs/apk/**/*.apk",
    "*/build/outputs/apk/**/*.apk",
    "build/outputs/bundle/**/*.aab",
    "*/build/outputs/bundle/**/*.aab",
)


def artifacts(folder: Path, *, since: float, limit: int = 8) -> list[tuple[str, int]]:
    """APK and AAB files under ``folder`` written since ``since`` (seconds since the epoch)."""
    found: list[tuple[float, str, int]] = []
    root = Path(folder)
    for pattern in _ARTIFACT_GLOBS:
        try:
            matches = list(root.glob(pattern))
        except (OSError, ValueError):
            continue
        for path in matches:
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime + 2 < since:
                continue
            try:
                shown = path.relative_to(root).as_posix()
            except ValueError:
                shown = str(path)
            found.append((stat.st_mtime, shown, stat.st_size))
    unique = {shown: (mtime, size) for mtime, shown, size in found}
    ordered = sorted(unique.items(), key=lambda item: -item[1][0])
    return [(shown, size) for shown, (_mtime, size) in ordered[:limit]]


def size_text(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


def now() -> float:
    return time.time()


def windows() -> bool:
    return os.name == "nt"
