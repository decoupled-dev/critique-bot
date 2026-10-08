---
name: automotive-safety
description: Safety- and security-relevant automotive code: ISO 26262, MISRA/AUTOSAR C++ themes, ISO 21434, defensive coding and deviations.
keywords: iso 26262, asil, asil-b, asil-d, functional safety, safety goal, freedom from interference, misra, misra c, misra c++, autosar c++14, iso 21434, iso/sae 21434, cybersecurity, polyspace, coverity, cppcheck, qac, helix qac, watchdog, fail-safe, deviation, safety-critical, safety mechanism
files: misra.json, .misra, deviations, safety, polyspace
---
# Automotive Functional Safety and Security Coding

## How to reason
- Determine the integrity level of the code: search for ASIL classification, safety requirements, safety manual, or partitioning notes. QM code next to ASIL code still must not interfere with it.
- Which safety goal or safety requirement does this code support? What is the safe state, and how is it reached on failure?
- What faults must be detected (invalid input, timeout, stale data, corrupted data, out-of-range, sequence errors), and what is the reaction time budget?
- Is behavior deterministic: bounded execution time, bounded memory, no unbounded loops or recursion, no allocation after init if the project mandates it?
- Which coding standard applies (MISRA C:2012, MISRA C++:2023, AUTOSAR C++14, CERT, project rules)? Find the checker config and existing deviation records before writing code.
- Freedom from interference: can this code corrupt memory, starve timing, or block communication of a higher-ASIL element? Check process/partition boundaries.
- Security (ISO/SAE 21434): is this an attack surface (IPC, network, CAN/vehicle bus, files, intents/binder)? Who can send input?
- Read the existing error-handling pattern and safety mechanisms (E2E protection, watchdog, plausibility checks) and reuse them.

## Do
- Validate every external input: range, length, enum membership, sequence counter, freshness/timestamp, checksum if the protocol provides one. Reject to a defined safe default.
- Define fail-safe defaults: on unknown state, error, or timeout, move to the documented safe state (e.g. degrade, disable feature, keep last-safe value for bounded time).
- Check every return value and error code; propagate or handle explicitly. Make ignored results visible (cast to void only where the standard allows and with a reason).
- Use fixed-width types (`uint8_t`, `int32_t`) for data with defined size; make conversions explicit and range-checked:
```c
int32_t clamp_speed(int32_t req_kph) {
    int32_t out = req_kph;
    if (req_kph < SPEED_MIN_KPH) { out = SPEED_MIN_KPH; }
    else if (req_kph > SPEED_MAX_KPH) { out = SPEED_MAX_KPH; }
    else { /* in range */ }
    return out;
}
```
- Bound all loops with a known maximum; add explicit iteration limits on retry/poll loops and timeouts on all waits.
- Allocate memory at init only where mandated; use static pools/fixed-capacity containers afterwards.
- Every `switch` has a `default` handling unexpected values; every `if/else if` chain ends in `else` where the standard requires it.
- Single point of exit per function if the project standard mandates it; otherwise follow the repo's existing style.
- Defensive programming against internal faults: plausibility checks on computed values, assertions that remain active only per project policy (do not rely on `assert` disabled in release).
- Watchdog/alive supervision: kick only from the healthy main path after the cycle's work completes, never from an independent timer that hides a stuck task.
- On Android/AAOS safety-relevant components: use timeouts on binder/HAL calls, handle service death (`linkToDeath`/death recipients), default the UI/feature to a safe state when vehicle properties are unavailable or stale, and do not depend on app process lifetime for safety functions.
- Security: least privilege (permissions, SELinux domains), secure defaults (features off, debug off in user builds), constant-time comparisons for secrets, no secrets/keys/credentials in code or logs, use platform keystore.
- Deviations: when a guideline cannot be followed, record in the project's deviation format: rule/guideline reference (as named by the checker), location, rationale, risk assessment, and approval needed. Keep suppression comments in the format the checker recognizes and the repo already uses.

## Avoid
- Undefined or unspecified behavior (signed overflow, uninitialized variables, out-of-bounds, implicit narrowing, unsequenced side effects) -> explicit checks and casts.
- Recursion -> iteration with bounded depth.
- Dynamic allocation, exceptions, or RTTI in contexts where the project forbids them -> static allocation, error codes.
- Unbounded waits (`while (!ready) {}`, blocking calls without timeout) -> bounded waits with timeout handling.
- Silent fallthrough or missing `default` -> explicit handling.
- Floating-point equality comparisons and unchecked float-to-int conversions -> tolerance and range checks.
- Suppressing checker findings without a recorded deviation -> fix or document.
- Citing specific rule numbers from memory -> reference the rule ID as reported by the configured tool.
- Claiming "MISRA compliant", "ASIL-D ready", or "21434 compliant" -> report only what the evidence shows (tool, version, ruleset, findings count).
- Logging VIN, location, keys, tokens -> redact.
- Trusting data from lower-integrity sources without checks -> validate at the boundary.

## Commands
- Run the project's configured analyzers; typical invocations (use the repo's config and ruleset):
  - cppcheck with MISRA addon: `cppcheck --addon=misra --enable=all --inline-suppr --error-exitcode=1 <src>` (add the repo's `misra.json` if present; rule texts file is licensed and project-supplied).
  - clang-tidy: `clang-tidy -p build <files>` with the repo `.clang-tidy` (cert-*, bugprone-*, misc-* checks).
  - Coverity: `cov-build --dir cov-int <build cmd>` then `cov-analyze --dir cov-int` per project setup.
  - Polyspace / Helix QAC: run via the project's scripts or CI job; do not invent CLI flags.
- Compile with strict warnings as the project does (`-Wall -Wextra -Werror`, plus `-Wconversion` if configured).
- Unit tests and coverage per project: e.g. `ctest --output-on-failure`, `gcovr --branches`; MC/DC from the qualified tool if the project uses one.

## Verify before COMPLETED
- State the integrity level assumed and its source (doc/file) or that it was not found.
- All inputs validated, return values checked, loops and waits bounded, safe state defined for each new failure path.
- Analyzer run on changed files: tool, ruleset, and new findings (fixed or with deviation drafts); or state the tool was unavailable.
- Tests cover nominal, boundary, invalid, and timeout/fault cases; coverage numbers reported if measurable.
- No new dynamic allocation after init, recursion, or unchecked casts where forbidden.
- No secrets, debug backdoors, or sensitive logging added.
- No compliance claims beyond the evidence; open items listed for safety/security review.
