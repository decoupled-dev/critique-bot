# Stage 1: Model protocol (web chat as a removable module, structured turns, native tool calls, many local models)

## Goal
Move the web-chat backend into a removable module that core crit never imports directly.
Replace the text-in/text-out model interface with
structured turns. Use the server's native tool calling, with a defensive fallback parser.
Support many local model families (Qwen, DeepSeek, GLM, Mistral/Devstral, gpt-oss, Llama,
Gemma, Kimi, MiniMax, Granite, Phi, …) on many local servers (llama.cpp, vLLM, SGLang,
Ollama, LM Studio, TGI, MLX-LM) through per-model profiles and per-server presets. The
provider interface is designed so cloud APIs can be added later (Stage 11) without engine
changes.

## Why
- Passing tools through the API `tools` field gave +2% on SWE-bench Verified (OpenAI
  GPT-4.1 guide).
- Qwen3.6-27B was evaluated with its own XML call template through vLLM's `qwen3_coder`
  parser. The trained surface is bash plus a str_replace editor called natively. Today crit
  asks Qwen for Responses-format JSON in the reply text, which also forces code into
  JSON-escaped strings.
- Local parsers do break: llama.cpp #19382 (duplicate params, then endless retry in
  opencode) and #20809 (calls landing in `reasoning_content`). Hence fallback parsing plus a
  breaker.
- Per-model profiles are what Aider, Factory and opencode all converge on.

## Scope
- **Isolate the web-chat backend** in `critique_bot/webchat/` behind the provider registry,
  so it can be deleted without touching anything else (list below).
- New model-layer types and the `ModelSession` interface.
- One adapter for now: OpenAI-compatible chat completions, which every local server
  speaks. Ollama's native `/api/chat` only if its OpenAI endpoint proves insufficient for
  tool calls.
- A **prompted tool mode** (tools described in the system prompt, calls parsed from the
  text) for models or servers without working native tool calling. It replaces today's
  web-chat text protocol and is a per-profile fallback, not a backend.
- Fallback tool-call parser, schema validation, breaker.
- Model profiles for many families and server presets; `crit doctor model` conformance probe.
- Engine loop switched to structured turns.

Out of scope: cloud providers (Stage 11; the interface allows them), grammar-constrained
decoding (a profile option in Stage 9, if evals justify it), deleting the web chat (it stays,
isolated; deleting it later is removing one folder).

### Web chat as a removable module
Rule: **no module outside `critique_bot/webchat/` imports it.** Core crit finds it through a
provider registry (`model_api.register_provider("browser", factory)`), which the package
registers when it is importable. Everything web-only moves in, or behind a hook it provides:

| Moves into `critique_bot/webchat/` | Lines (approx.) | Notes |
|---|---|---|
| `browser.py`, `chat_client.py`, the browser half of `provider.py`, `setup_ui.py` | ~5,800 | Edge/Playwright driving, selectors, the click-to-pick setup page, `crit setup` |
| Web-UI parts of `setup_wizard.py`, `welcome.py`, `config_cmd.py`, `config_prompts.py` | — | the "Web UI" first-run branch and its questions, registered as a wizard step by the package |
| Web-only config fields (`selectors`, `model_dropdown*`, `cdp_url`, `filter_chat_page`, `page_network`) | — | parsed by a config section the package registers; core `config.py` keeps only shared fields |
| Site/proxy sign-in for the chat page | — | inside the package |
| Turn/time compaction triggers and the prose heuristics (`_hands_back`, `_stalls`, `_refuses`, `_cut_fabricated`, chrome stripping) | — | move to the web-chat provider (its `text_protocol` adapter), not the engine |
| `playwright` dependency | — | optional extra `crit[webchat]`; the base install has no Playwright |
| Tests for all of the above | — | move to `tests/webchat/`, skipped when the package or Playwright is missing |

Keep in core: redaction, `allowed_hosts` and the egress policy for crit's own traffic, the
job queue and worker (on any provider), review mode, GitLab/Jira integrations.

**Removability check:** `tests/test_webchat_removable.py` copies the package source to a temp
dir without `webchat/`, imports every core module, and runs the core test suite against it.
CI runs the same job. A config with `backend: browser` when the package is missing gets one
clear message (install `crit[webchat]`, or switch to a local model) instead of an import error.

## Design

### Types (`model_api.py`, models layer)
```python
@dataclass
class ToolCall:        # what the model asked for
    id: str
    name: str
    arguments: dict[str, Any]
    raw: str = ""                 # original text, for transcripts and repair messages

@dataclass
class Message:         # one history entry, provider-neutral
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)   # assistant only
    tool_call_id: str = ""                                      # tool only
    reasoning: str = ""           # kept when the profile says preserve_thinking

@dataclass
class Usage:
    prompt: int = 0; completion: int = 0; cached: int = 0

@dataclass
class Turn:            # one model reply
    text: str
    tool_calls: list[ToolCall]
    reasoning: str
    finish: Literal["stop", "tool_calls", "length", "error"]
    usage: Usage
    parse_notes: list[str]        # "recovered from reasoning_content", "dedup param x"

class ModelSession(Protocol):
    def complete(self, messages: list[Message], tools: list[ToolSpec], *,
                 on_text: Callable[[str], None] | None = None) -> Turn: ...
    def stop(self) -> bool: ...
    def close(self) -> None: ...
```
`ChatSession.send(str) -> str` is deleted along with the web-chat backend. Helper
conversations (`agent_helpers.py`) move to `complete()` with no tools.

### Provider interface (models layer), built for later cloud adapters
```python
class Provider(Protocol):
    id: str                                   # "openai-compat", later "anthropic", "gemini", ...
    capabilities: ProviderCaps                # native_tools, parallel_tools, reasoning_field,
                                              # prompt_cache (auto | explicit | none), max_context,
                                              # usage_fields, needs_api_key
    def list_models(self) -> list[ModelInfo]
    def session(self, model: str, profile: Profile) -> ModelSession
```
Everything provider-specific (wire format, cache hints, reasoning field names) lives in the
adapter; the engine only sees `Message`/`Turn`/`ToolSpec`. Stage 11 adds cloud adapters
against this interface with no engine changes; the contract tests below are written so a
new adapter only has to pass them.

### Adapters now (`providers/`)
| Module | Wire | Notes |
|---|---|---|
| `providers/openai_compat.py` | `POST /v1/chat/completions`, `stream: true`, `tools`, `tool_choice: auto` | Grows out of `LocalModelClient`; keeps stdlib HTTP, API-key handling, `allowed_hosts`; accumulates streamed `tool_calls` deltas; reads `reasoning_content` / `reasoning` / `<think>` blocks per profile; usage; `id_slot` + `cache_prompt` for llama.cpp |
| `providers/prompted_tools.py` | wraps any `ModelSession` | for `tool_mode: prompted`: renders tools into the system prompt in the model family's preferred format (Hermes JSON, Qwen XML, or a plain JSON block) and parses calls from the text with `tool_parse`; returns `Turn` |

Selection: `config.backend` is just `local` (the server URL) for now; `profile.tool_mode`
is `native`, `prompted`, or `auto` (decided by the doctor probe). Prompted mode works on any
server, so a broken server parser can always fall back to it.

### Server presets (`profiles/servers/*.json`)
| Server | Detect | Native tool calls need | Cache |
|---|---|---|---|
| llama.cpp `llama-server` | `/props` | `--jinja` (+ `--chat-template-file` when the GGUF's template lacks tools); KV cache f16/q8_0 | `cache_prompt`, `id_slot` |
| vLLM | `/version` | `--enable-auto-tool-choice --tool-call-parser <family parser>` + `--reasoning-parser` where one exists | automatic prefix caching |
| SGLang | `/get_server_info` | `--tool-call-parser <family>` | radix cache |
| Ollama | `/api/version` | model template with tools (`ollama show --template`) | keep-alive |
| LM Studio | `/api/v0/models` | tool-use capable model | — |
| TGI | `/info` | `--enable-tool-calling`-style flags per version | — |
| MLX-LM server (Mac) | `/v1/models` + header | often prompted mode | — |

Each preset says how to launch the server for agent use, and `crit doctor model` checks the
running server against it.

### Speculative decoding for edits (presets, measured)
Whole-file rewrites and long edits mostly copy text that is already in the prompt.
N-gram (prompt-lookup) speculative decoding guesses those tokens from the prompt and
verifies them in one batch, which is the open, no-extra-model version of Cursor's fast
apply. Presets include it where the server supports it:
- **vLLM:** n-gram speculative decoding (`--speculative-config` with the `ngram` method and a
  lookup window), or a small draft model of the same family.
- **llama.cpp:** a draft model (`-md` with a small same-family GGUF) and the server's
  lookup/n-gram speculation options in the build being used.
- **SGLang:** its speculative options per version.

Flags change between releases, so each preset pins the server version it was tested with,
and `crit doctor model` reports whether speculation is active. This is measured, not assumed:
work item 12 records tokens per second for a whole-file rewrite of a 600-line file and for
normal agent turns, with speculation on and off. Speculation stays in a preset only if it
speeds up rewrites without slowing normal turns by more than 5%.

### Fallback parser (`tool_parse.py`, models layer)
Runs when `tool_mode: native` but the turn has no structured `tool_calls` while the text or
reasoning contains call markup, or when structured arguments fail to parse. It recognizes, in order:
1. Hermes `<tool_call>{json}</tool_call>`
2. Qwen XML `<function=NAME><parameter=K>V</parameter>…</function>` (duplicate keys: keep the
   last, and record a note)
3. Fenced ```json {"name":…,"arguments":…}``` blocks
4. Today's Responses-format `{"output":[…]}` (from `openai_format`)

Then: lenient JSON repair (reuse `agent._repair_json`, moved here), and schema validation
against the `ToolSpec` (a small validator covering type, required, enum, min/max; no new
dependency). Schema errors go back to the model as a tool result:
`{"ok": false, "error": "argument 'path' is required; you sent: {...}"}`.

### Breaker (engine)
- Hash each (name, arguments) pair, or the raw text when the call is malformed.
- 2nd identical malformed call: tool result says what is wrong and shows one valid example.
- 3rd: switch this session to `tool_mode: text` for the rest of the task (note to UI and to
  the event log), or stop with `exit_reason=protocol` if the profile forbids switching.
- Identical *valid* calls with identical results three times in a row: tell the model it is
  repeating itself (the existing `_call_key` logic, generalized).

### Profiles (`profiles/*.json` built-in, overridable in `~/.crit/profiles/`)
```json
{
  "id": "qwen3.6-27b",
  "match": ["qwen3.6-27b*", "Qwen3.6-27B*"],
  "tool_mode": "native",
  "sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "presence_penalty": 0.0},
  "preserve_thinking": true,
  "edit_format": "str_replace",
  "edit_fallback": "whole_file",
  "effective_context": 32768,
  "tool_output_cap_tokens": 6000,
  "shell_timeout_s": 60,
  "notes": "vLLM: --tool-call-parser qwen3_coder --reasoning-parser qwen3 --enable-auto-tool-choice; llama.cpp: --jinja, KV cache q8_0 or f16 (not q4_0)"
}
```
Profiles are per **model family** with per-size overrides, because tool templates,
reasoning output and sampling are set by the family. Built-ins at first (each one verified
with `crit doctor model` before it ships; values come from the model card and cite it in `notes`):

| Family | Examples | Expected tool mode | Things the profile must handle |
|---|---|---|---|
| Qwen3 / 3.5 / 3.6, Qwen3-Coder | 27B, 35B-A3B, 30B-A3B, 480B | native (`qwen3_coder` / hermes parsers) | XML call format; thinking on/off; `preserve_thinking` |
| DeepSeek V3.x / R1 / Coder-V2 | V3.2, R1 distills (Qwen/Llama based), Coder-V2-Lite | native for V3.x; R1 distills often prompted | `reasoning_content` or `<think>` blocks; R1-style models should not get reasoning echoed back; distills inherit the base model's template |
| GLM-4.x | GLM-4.5/4.6/4.7, GLM-4.5-Air | native (`glm45` parser in vLLM/SGLang) | interleaved thinking |
| Mistral / Devstral | Devstral Small 2, Mistral Small 3.x, Codestral | native (`mistral` parser) | tool-call ids of a fixed length; `[TOOL_CALLS]` format |
| gpt-oss | 20B, 120B | native (harmony format via server) | reasoning effort setting; harmony channels |
| Llama 3.x / 4 | 3.3-70B, 4 Scout/Maverick | native (`llama3_json` / `pythonic`) | weaker multi-step tool use, so tighter caps |
| Gemma 3 / 3n | 12B, 27B | prompted (no native tool tokens) | no system role in some templates |
| Kimi K2, MiniMax M2 | — | native (family parsers) | large MoE, usually served remotely on the LAN |
| Granite, Phi-4 | — | native or prompted per version | small-model caps |
| `generic` | anything unknown | `auto` (probe decides) | conservative caps, 16K effective context |

Resolution order: config `profile`, then the first `match` on the model name (plus the GGUF
metadata / server-reported architecture when the name is ambiguous), then `generic`. A user
profile in `~/.crit/profiles/` can override any field, and `crit doctor model --write-profile`
saves the probe's findings as a starting user profile for an unknown model.

### Server presets and `crit doctor model`
`crit doctor model` checks:
1. The server is reachable, and the model is listed.
2. The server type (llama.cpp `/props`, vLLM `/version`, LM Studio `/api/v0/models`).
3. Context window and KV-cache type, where the server reports them; warns on `q4_0` KV.
4. **Conformance probe:** 12 synthetic turns covering a single call, parallel calls, a code
   argument with quotes, backslashes and newlines, a call after a thinking block, a
   no-call answer, and a long argument (4 KB). Reports native success, fallback recoveries
   and failures, then recommends `native` or `text`.
5. The launch command line to use (the preset), printed for copy-paste.

Results are cached per (server, model) in `~/.crit/cache/doctor.json`, and the profile can
use them (`tool_mode: auto` = trust the probe).

### Engine changes (`agent.py` → uses `ModelSession.complete`)
- `_TaskRun` keeps `messages: list[Message]` and sends `registry.specs()` in a **fixed
  order** (sorted by name, built-ins first) for the whole session.
- Tool results become `Message(role="tool", tool_call_id=…)`. Their content is the existing
  `format_tool_result` output, capped by profile.
- Parallel tool calls in one turn are allowed. Read-only ones run concurrently; writes run
  in order.
- The loop's stop conditions read `Turn.finish` instead of parsing prose. The web-page
  prose heuristics (`_hands_back`, `_stalls`, `_refuses`, `_cut_fabricated`) are deleted;
  prompted mode keeps only a "no call and no final answer" check.

## Work items
1. **Isolate the web-chat backend** (table above) in its own commits: provider registry in
   `model_api`; move the modules into `critique_bot/webchat/`; config section and wizard-step
   hooks; move tests to `tests/webchat/`; `webchat` optional extra; layer test support for the
   subpackage; the removability test and CI job; packaging keeps bundling it for the release
   builds. The suite stays green after each commit.
2. Add types to `model_api.py` and the `Provider` interface; add `tests/test_model_types.py`.
3. `providers/openai_compat.py`: port `LocalModelClient`; streamed tool-call delta
   accumulation; reasoning fields per profile; usage; `id_slot` pinning (one slot per
   session id); non-streaming fallback (`stream: false`) when a profile asks or after a
   stream parse failure.
4. `tool_parse.py`: recognizers (Hermes JSON, Qwen XML, Mistral `[TOOL_CALLS]`, Llama
   pythonic/JSON, fenced JSON), repair and the schema validator. Move JSON-repair helpers
   out of `agent.py`; delete the Responses-format protocol code (`openai_format.py`) once
   prompted mode covers its cases.
5. `providers/prompted_tools.py` with per-family prompt formats.
6. `profiles/` + `profiles.py` (family profiles, size overrides, server presets,
   resolution, validation, `crit config --profile`).
7. Engine migration: `_TaskRun` and `run_read_only_task` onto `complete()`; breaker; parallel
   read-only calls; helper conversations onto `complete()` with no tools.
8. `crit doctor model` (apps) with `--write-profile`, plus `docs/local-models.md` (every
   server preset, with the launch line).
9. **Model matrix:** run the doctor probe and the quick eval suite on at least one model
   per family in the table that fits the dev machine (plus any LAN-served large ones), and
   publish the matrix in `plan/results/01/models.md`: tool mode, probe score, quick-suite
   pass@1, tokens/s.
10. Update `tests/test_layers.py` LAYERS for the new modules; update `docs/architecture.md`.
11. Full eval run on: Qwen3.6-27B native on llama.cpp and vLLM, and prompted on llama.cpp.
    Record in `plan/results/01/`.
12. Speculative-decoding presets for vLLM, llama.cpp and SGLang; a benchmark script
    (`scripts/eval/spec_bench.py`) for rewrite and normal-turn throughput with speculation
    on and off; results in `plan/results/01/speculation.md`.

## Tests
- Recorded-stream fixtures: real SSE captures from llama.cpp, vLLM, SGLang and Ollama, for
  at least Qwen3.6-27B, Qwen3-Coder, a DeepSeek model, GLM-4.x, Devstral and gpt-oss (single
  call, parallel calls, call inside reasoning, duplicate params, broken JSON). The parser
  output must be stable.
- Fuzz: random truncation and escaping of argument strings, to make sure the parser never
  crashes and always returns either a call or a clear error.
- Engine tests run on a scripted `ModelSession` that returns `Turn`s (replacing the scripted
  `send` sessions).
- **Provider contract tests** (`tests/provider_contract.py`): any adapter must pass them, so
  Stage 11 cloud adapters plug in by passing the same suite.
- Removability: the core suite passes with `webchat/` deleted; no core module imports
  `webchat` or `playwright` (a test parses every import).

## Completion targets
- [ ] Web chat isolated: no core module imports `webchat` or `playwright`; core suite green
      with the folder deleted (test + CI job); base install has no Playwright; the web-chat
      tests pass with `crit[webchat]` installed
- [ ] Engine has no code path that parses tool calls from text except through
      `prompted_tools` / `tool_parse`
- [ ] `crit doctor model` passes 12/12 native probes for Qwen3.6-27B on vLLM
      (`qwen3_coder` parser) and ≥ 11/12 on llama.cpp `--jinja`, with fallback recovering the rest
- [ ] Built-in profiles for every family in the table; the model matrix published with
      ≥ 6 families measured (Qwen, DeepSeek, GLM, Mistral/Devstral, gpt-oss, one more)
- [ ] Malformed-call rate on the suite < 1% of model turns for every family run in native mode
- [ ] No endless retry: breaker test covers the #19382 duplicate-param stream
- [ ] pass@1 (native) ≥ pass@1 (baseline text protocol) + 3 points on the full suite, same
      model and server; completion tokens per solved task ≤ baseline
- [ ] The adapter passes the provider contract tests
- [ ] Speculation benchmark recorded; presets enable it only where whole-file rewrites get
      ≥ 1.5× faster and normal turns slow down by ≤ 5%

## Risks
- **Thinking models and history:** some servers reject `reasoning` echoed back, and R1-style
  models should not get it back. Profile flag `preserve_thinking` decides; test both.
- **Parallel calls on small models:** they can over-call. Profile `parallel_tool_calls:
  false` for models that misbehave.
- **Server parser drift:** parsers change between llama.cpp/vLLM releases. The recorded
  fixtures carry the server version, and the model matrix is rerun at each release.
- **Size of the engine migration:** `agent.py` is 4,100 lines. Moving the web-only heuristics
  into the web-chat provider first makes it smaller. Do the turn migration behind a flag (`CRIT_TURNS=1`), switch the
  default after the eval, and delete the old path in Stage 5.
