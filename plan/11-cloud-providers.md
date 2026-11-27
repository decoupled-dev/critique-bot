# Stage 11 (future): Cloud providers

## Goal
Add cloud model APIs as adapters behind the Stage 1 `Provider` interface, with no engine
changes. Keep crit local-first: a cloud model is something a user or an agent file turns
on, it is never a default, and it is always subject to redaction, egress and taint rules.

## When
After M2 (public beta), or earlier if users ask for it. Nothing in Stages 1–10 may assume
a cloud model exists. Stage 1's provider contract tests are the gate for every adapter here.

## Scope
| Adapter | Wire | Notes |
|---|---|---|
| `openai_compat` (exists) | chat completions | already covers OpenAI, OpenRouter, Groq, Together, Fireworks, DeepSeek API, Mistral API, any hosted vLLM; needs an API-key store and per-host profiles |
| `anthropic` | Messages API, SSE | `tool_use`/`tool_result` blocks; explicit `cache_control` on system + tools; thinking blocks |
| `openai_responses` | Responses API | only if a model needs it (reasoning items, hosted tools off) |
| `gemini` | native `generateContent` or the OpenAI-compatible endpoint | decide by tool-call reliability in the contract tests |
| `bedrock` / `vertex` | AWS / GCP SDK auth | enterprise; behind an optional extra |

## Design
- **Credentials:** `crit auth add <provider>` stores keys in the OS keychain (keyring as an
  optional extra), or in `~/.crit/credentials.json` (mode 0600) as the fallback. Never in a
  project file; env vars are supported for CI.
- **Egress:** every cloud host must be in `allowed_hosts`; `egress: strict` blocks cloud
  providers unless they are listed. The audit log records every request (host, model,
  tokens; never content).
- **Privacy routing:** an agent file can set `privacy: local-only | redacted-cloud |
  cloud`. `redacted-cloud` sends only redacted context (the existing redaction, extended
  with a preview of what will be sent on first use). Tainted sessions cannot switch to a
  cloud model mid-task.
- **Cost:** a per-model price table (user-editable), live cost in `/status`, and money
  budgets per task and agent (Stage 9.4).
- **Caching:** the adapter maps the Stage 3 fixed prompt order onto provider caching
  (Anthropic explicit breakpoints; OpenAI/DeepSeek automatic prefix caching) and reports
  cache hits in the same usage fields.
- **Profiles:** cloud model profiles go in the same `profiles/` format (edit format,
  effective context, output caps). Frontier models get larger caps and a looser compaction
  budget.

## Work items
1. Credentials store and `crit auth`.
2. `anthropic` adapter; contract tests with recorded fixtures.
3. Per-host profiles for the OpenAI-compatible cloud hosts.
4. `gemini` adapter (native or compatible, chosen by the contract tests).
5. Privacy routing in agent files; cost table and budgets.
6. Optional `bedrock` / `vertex` extras.
7. Eval: the suite on one frontier model per provider, to publish crit-vs-opencode numbers
   for cloud models as well.

## Completion targets
- [ ] Every adapter passes the provider contract tests without engine changes
- [ ] No cloud request leaves without an `allowed_hosts` entry (test)
- [ ] `redacted-cloud` never sends a configured redaction term (test with seeded secrets)
- [ ] Cost shown live and budgets enforced
- [ ] Suite results published for at least two cloud models

## Risks
- **Scope pull toward cloud features** (hosted tools, cloud sandboxes): out of scope; crit's
  edge is the local harness.
- **Terms of service and data handling:** the docs state what each provider receives, and
  which settings keep data local.
