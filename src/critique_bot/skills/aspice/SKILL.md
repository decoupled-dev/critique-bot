---
name: aspice
description: Apply Automotive SPICE (3.1/4.0) to code changes: traceability, verification evidence, change impact, work products and baselines.
keywords: aspice, automotive spice, swe.1, swe.2, swe.3, swe.4, swe.5, swe.6, sup.1, sup.8, sup.9, sup.10, man.3, traceability, bidirectional traceability, requirement id, software requirements specification, unit verification, integration test, qualification test, change request, impact analysis, baseline, review record, verification criteria
files: requirements, docs/requirements, traceability, doors, polarion, reqif
---
# Automotive SPICE for Code Changes

## How to reason
- First find how the repo already does traceability: search for requirement ID patterns in code comments, test names/annotations, commit messages, and docs (e.g. requirements exports, ReqIF, CSV, Polarion/DOORS IDs, `@Requirement`/`@Trace` style tags). Copy the exact format; never invent one.
- Which requirement(s) does this change implement or affect? If none can be identified in the repo or the task, say so and ask; do not fabricate IDs.
- What work products does this change touch: software requirements (SWE.1), architecture (SWE.2), detailed design and code (SWE.3), unit tests (SWE.4), integration tests (SWE.5), qualification tests (SWE.6)?
- Is there a change request / problem report ID (SUP.10 / SUP.9) in the task, branch name, or commit convention? Reference it.
- What is the impact: other components, interfaces, configuration, calibration data, other variants, safety/security relevance?
- What verification evidence will a reviewer or assessor need, and which of it can be produced now (tests, coverage, static analysis output)?
- Which baseline/version is affected (SUP.8): release branch, tags, variant configuration?

## Do
- Process areas, for orientation:
  - SYS.2-SYS.5: system requirements, system architecture, system integration test, system qualification test. Software work must trace up to system requirements.
  - SWE.1: software requirements are specified, analyzed (feasibility, testability, consistency), and traced to system requirements; each has verification criteria.
  - SWE.2: architecture with components, interfaces, dynamic behavior, resource consumption; traced to SWE requirements.
  - SWE.3: detailed design and units; units traced to design; code consistent with design.
  - SWE.4: unit verification per a strategy: unit tests, static analysis, code review, coverage; results recorded.
  - SWE.5: integration in a defined order; integration tests against architecture/interfaces.
  - SWE.6: tests against software requirements on the integrated software; results traced.
  - SUP.1 quality assurance, SUP.8 configuration management, SUP.9 problem resolution, SUP.10 change request management, MAN.3 project management (scope, estimates, progress).
- Bidirectional traceability: requirement -> design element -> code unit -> test case -> test result, and back. Each link must be navigable both ways in the repo's tooling.
- Mark traces exactly as the repo does. Examples of common forms (use only if they match existing usage):
```c
/* Implements: SWRS-1234 */
TEST(SpeedLimiter, ClampsAboveMax) { /* Verifies: SWRS-1234 */ }
```
  Commit message: `[CR-567] Clamp speed request (SWRS-1234)` if that is the established pattern.
- Keep consistency: when code behavior changes, update the linked design text and tests in the same change, or list them as required follow-ups.
- Verification criteria: each requirement you touch should have an observable pass/fail condition; tests assert that condition, including boundary values and error cases.
- Coverage: report statement and branch coverage for changed units; MC/DC is typically expected for higher-integrity code (e.g. ASIL C/D per ISO 26262 recommendations); state what the project's verification strategy requires rather than assuming.
- Static analysis: run the configured tools (compiler warnings, clang-tidy, MISRA checker, Coverity/Polyspace if available) and record findings for changed files, with justification for any accepted deviation.
- Review evidence: summarize what was changed, why, and which checks passed, in a form that can be attached to the review record.
- Change impact analysis: list impacted requirements, interfaces, modules, tests, documents, variants, and whether regression scope expands.
- Configuration management: changes go through version control with meaningful messages; do not modify baselined/tagged artifacts or generated files by hand; update version numbers only per the project's process.
- Problem resolution: for bug fixes, record root cause, affected versions, fix, and the regression test that reproduces the defect.

## Avoid
- Inventing requirement IDs, CR numbers, or document references -> use only IDs found in the repo or task; otherwise write "requirement ID not identified" and flag it.
- Introducing a new tagging syntax -> follow the existing one exactly.
- Code-only changes that silently desynchronize design docs or test specs -> update them or list the gap.
- Tests without traceable intent (no ID, vague name) where the repo traces tests -> name and tag per convention.
- Claiming coverage, compliance, or capability levels without measured evidence -> report actual numbers or "not measured".
- Deleting or weakening failing tests to pass -> fix the code or raise a problem report.
- Bundling unrelated changes in one commit -> one change request per logical change.
- Editing generated code (from models/ARXML/IDL) instead of the source model -> change the source, regenerate.

## Commands
- Find trace conventions: `git grep -nE "(REQ|SWRS|SRS|SWE|SYS)[-_][0-9]+"` and `git log --oneline -n 50` (PowerShell: same git commands; use `Select-String -Pattern` instead of grep for files outside git).
- List changed files for impact analysis: `git diff --name-only <base>...HEAD`.
- Run the project's unit/integration tests and coverage task as configured (e.g. `ctest`, `gcovr --branches`, `./gradlew test jacocoTestReport`, `atest`), and the static analysis target if defined.
- Search for links to a specific requirement before changing it: `git grep -n "<REQ-ID>"`.

## Verify before COMPLETED
- Report: requirement IDs implemented/affected (only real ones), CR/problem report ID if given.
- Traceability updated in code comments/tests/commit message per existing convention; no orphan new code or tests where the repo traces them.
- Tests added/updated with verification criteria covered; command and results reported.
- Coverage for changed units reported with numbers if a tool exists; otherwise state not measured.
- Static analysis run on changed files; new findings fixed or listed with justification.
- Change impact list: requirements, design docs, interfaces, tests, variants; docs needing update named explicitly.
- Open gaps (missing IDs, unmeasured coverage, unrun HIL tests) stated plainly.
