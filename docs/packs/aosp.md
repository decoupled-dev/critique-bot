# The AOSP pack

The AOSP pack is for Android platform and Android Automotive (AAOS) work: device logs, native
crashes, SELinux denials, Soong builds and the tests that cover a change. It ships with crit
as built-in content. It adds three tools, three agents and a sandbox preset. Most of what it
knows is in code: the tools pick defaults, parse logs and put hints in their results. Very
little of it is in the prompt.

## Turning it on

The pack is off until you turn it on. Add it to your own `.crit/settings.json`:

```json
{ "packs": ["aosp"] }
```

When crit opens a tree that looks like AOSP (`build/envsetup.sh` plus `Android.bp` files) and
no `packs` setting exists, it suggests the pack once. It records that it asked in
`.crit/cache/packs-offered.json`. Set `"packs": []` to never be asked.

`packs` is a security key, like `sandbox` and `allow`. A cloned repository's committed
settings file cannot turn a pack on, because the pack adds a tool that reaches a device and
widens the sandbox (see below).

While the pack is active:

- The pack's tools are added to the session's tool list. The list is fixed when the session
  starts, so the prompt cache stays valid.
- Its agents can be used with `/agent` and `delegate`.
- The sandbox uses the `aosp` preset unless you named a preset yourself. That preset makes
  `out/`, `~/.ccache`, `~/.cache/ccache` and `~/.cache/bazel` writable, and forwards the adb
  port (5037). See [security.md](../security.md#the-sandbox).

To replace a pack tool, put a tool with the same name in your own `~/.crit/tools/` (or a
trusted project's `.crit/tools/`). Your tool wins.

## Tools

The pack has one tool per area. Each tool takes an `action` argument, so a small model
chooses among three tools rather than twenty.

### `triage`: from a log to the code, the module and the commit

| action | input | what it finds |
|---|---|---|
| `crash` | logcat (`FATAL EXCEPTION`) or a pasted stack | the root cause: the last `Caused by`, and its first frame in non-framework code |
| `anr` | logcat (`ANR in`) plus `traces` (`/data/anr/anr_*`) | the main thread's state and frame; the thread holding the lock it waits for; deadlocks |
| `tombstone` | a tombstone, or logcat `DEBUG` lines | the signal or abort message, and the first frame outside libc, liblog, libbase and libc++ |
| `selinux` | dmesg or logcat | the denial that blocks; the minimal allow rule; the `.te` file to edit |
| `build` | a build log (`m`, ninja, Soong, javac, clang, lld) | the first error, the missing symbol or package, the failing module |

Every action runs the same pipeline:

1. **Read.** `path` is a file in the workspace, of any size, or `text` is the pasted lines.
   Files over 8 MB are read through the `log` tool's engine, which finds the events and
   queries the lines it needs.
2. **Extract** the failure (see the table).
3. **Map it to source.**
   - Stack frames and `file:line` go to files. A Java frame must match the class's package.
   - Log messages go to the code that prints them. This is skipped for messages the runtime
     writes, such as NPE and index errors.
   - Function names go to definitions.
   - When crit has a [code index](../indexing.md), the index is used. Without one, crit
     scans the workspace for at most 3 seconds.
4. **Find the owning Soong module** with the same `Android.bp` reader that `code_nav
   module_of` uses. It follows filegroups and defaults.
5. **Rank suspect commits.**
   - The blame of the failing line.
   - For a null dereference, the commits that changed how the null field is set.
   - For a missing symbol or module, the commit that removed or renamed it.
   - Recent changes to the file and to its module.
6. **Write a short report**, with a verdict and next steps.

**The refusal guard.** Sometimes nothing in the failure resolves to this tree: the code is
in another project, a prebuilt or an OEM app. Then the report says `source: NO CANDIDATE`
and `verdict: refuse to guess`. A same-named class in another package does not count as a
match.

**Native crashes.** crit symbolizes frames when it can, using the unstripped libraries in
`out/target/product/*/symbols` and `llvm-addr2line` from `prebuilts/clang` (or `addr2line` on
PATH). This is the same data `development/scripts/stack` uses. Without those files, the
report says `unsymbolized`. It then uses the tombstone's function names, and the `file:line`
that a `CHECK` puts in the abort message.

**SELinux.** The blocking denial is one that is enforced (`permissive=0`) and comes from a
process init saw exit. The rule crit writes has exactly the denied permissions: no
wildcards, no macros that grant more. When the target is a generic label (`sysfs`,
`vendor_file`, `default_prop`, ...), crit does not offer the rule. It gives the
`genfscon`/`file_contexts` line and the new type to add instead.

### `adb`: the device

| actions | asks? |
|---|---|
| `devices`, `logcat`, `getprop`, `dumpsys`, `services`, `packages`, `ps`, `settings_get`, `dmesg` | no (read-only) |
| `bugreport`, `pull` | like a command: they write a file in the workspace |
| `push`, `install`, `uninstall`, `reboot`, `settings_put`, `flash` (fastboot) | **every time, in every mode, auto included, even with `confirm_risky: false`** |

- `adb` typed through `run_command` follows the same rule: only a fixed list of read-only forms runs unasked (`adb devices`, `adb logcat -d`, `adb shell getprop`/`dumpsys <service>`/`pm list`/`settings get`/`ls`/`cat`, ...; no quotes or shell characters, which the device's shell would run). Any other adb command that reaches the device asks every time, in every mode; `pull` and `bugreport` ask like a command.
- `serial` picks a device when several are connected (it sets `ANDROID_SERIAL`).
- `logcat` dumps the log (`-d`, newest `lines`, 5000 by default) into
  `.crit/cache/device/logcat-<time>.txt` and returns a summary of its crashes, ANRs and
  errors, not raw lines. The next step is `triage` on that file.
- Every value that reaches the device's shell (a property, a service, a settings key) must
  match a fixed pattern of letters, digits and `._-`. No value may start with `-`. A request
  like `{"name": "x; reboot"}` is refused before anything runs.

What `adb` returns is device output. crit treats it as untrusted: it is wrapped in an
`<untrusted>` block and marks the session tainted as a device log
([taint](../security.md#taint)). `devices` is the exception.

### `aosp_build`: lunch, m, atest, and the tests for a change

| action | what it does |
|---|---|
| `lunch` | `lunch <target>`; with no target, lists the tree's lunch targets (car targets first) |
| `m` | `m <modules>`; with no modules, the modules that own the files changed in git and in this task. It never runs `m` alone, which builds the whole platform (hours). |
| `atest` | `atest <tests>`; with no tests, the top tests `tests_for` picks |
| `tests_for` | the test modules for some files (default: the changed files), and the `atest` line |

`lunch`, `m` and `atest` run in the session's shell, exactly like `run_command`:
`envsetup.sh` is sourced, the lunch target is kept, and the build's output is summarized.
They are also asked about like `run_command`, so a build inside the sandbox runs unasked in
auto mode.

`tests_for` reads two sources and nothing is built:

- The `TEST_MAPPING` files in the changed file's folder and every folder above it, and the
  files they `import`. Presubmit groups come first.
- The `Android.bp` test modules near the owning module (its folder, and the `tests/` folders
  of the folders above). A test counts when it instruments the owning app
  (`instrumentation_for`) or builds against the owning library (`static_libs`, `libs`, ...,
  also through `defaults`).

## Agents

| agent | for |
|---|---|
| `aosp-debugger` | the root cause of a crash, ANR or native crash, down to file:line, module and suspect commit. It reads and does not edit. |
| `aosp-build-fixer` | a broken build: fix the first error at the right place, rebuild only the affected modules, run the selected tests |
| `selinux-helper` | the minimal policy change for a denial, or why an allow rule is the wrong fix |

Use one with `/agent aosp-debugger`, or delegate to it with
`delegate {"tasks": [{"agent": "aosp-debugger", "brief": "..."}]}`.

## Writing a pack tool

Pack tools are ordinary [manifest tools](../extending.md#one-tool-several-actions) with
`actions`. Only crit's own packs may give an action a Python `handler`. Your own manifest
tools use `argv` actions.

## Measuring it

The AOSP tasks are in `tests/eval_tasks/aosp-*`, and `tests/eval_tasks/AOSP-SUITE.md`
describes them. To compare the same model with and without the pack:

```bash
crit eval --tag aosp --runs 3 --config ~/.crit/qwen.json --out .crit/eval/no-pack
crit eval --tag aosp --runs 3 --config ~/.crit/qwen.json --packs aosp --out .crit/eval/pack
crit eval compare .crit/eval/no-pack .crit/eval/pack
```
