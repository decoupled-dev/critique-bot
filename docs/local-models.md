# Local models: servers, profiles and `crit doctor model`

crit talks to any OpenAI-compatible server (`POST /v1/chat/completions`, `GET /v1/models`).
What differs between model families (tool-call format, sampling, reasoning, usable context)
lives in **model profiles**, and what differs between servers (launch flags, tool-call
parsers, caching, speculative decoding) lives in **server presets**. Neither needs code
changes: both are JSON files you can override.

The basic config (see [config.json.md](config.json.md#local-model-backend-local)):

```json
{
  "backend": "local",
  "url": "http://127.0.0.1:8080",
  "model": "qwen3.6-27b"
}
```

Then check the server once:

```bash
crit doctor model
```

## `crit doctor model`

It checks, in order:

1. **Reachable**: the server answers and lists the model (`GET /v1/models`).
2. **Server type**: llama.cpp (`GET /props`), SGLang (`/get_server_info`), vLLM (`/version`),
   Ollama (`/api/version`), LM Studio (`/api/v0/models`), TGI (`/info`), OpenRouter (by host),
   else a generic OpenAI-compatible server (MLX-LM shows up as generic). It shows the version,
   the context window, and where the server reports them the KV cache type (a `q4_0` cache
   gets a warning: tool-call arguments and long code degrade), the tool-call parser and
   whether speculative decoding is on. For llama.cpp it warns when the loaded chat template
   has no tools section (start with `--jinja`, and `--chat-template-file` if the GGUF's own
   template lacks tools).
3. **Profile and launch line**: the profile crit picked and the preset's launch command for
   this server, filled in with the model, context and the profile's parsers.
4. **Conformance probe**: 12 small native tool-call turns, each scored `native-ok` (the server
   returned a correct structured call), `recovered` (the call was in the text or the
   reasoning and crit's fallback parser found it) or `failed`:

   | # | Turn | Checks |
   |---|---|---|
   | 1 | a single call | `read_file` with the right path |
   | 2 | two parallel calls | two `read_file` calls in one reply |
   | 3 | code with quotes, backslashes, newlines | `write_file` content byte-exact |
   | 4 | a call after thinking | the right file after reasoning |
   | 5 | a no-call answer | plain text, no tool call |
   | 6 | a 4 KB argument | a 4.2 KB `write_file` content, exact |
   | 7 | a call that uses a tool result | follows the previous result |
   | 8 | an enum argument | `mode: "regex"` |
   | 9 | integer and boolean arguments | `timeout_s: 120` as a number, `background: false` |
   | 10 | a non-ASCII argument | exact Unicode text |
   | 11 | an array of objects | `edit_file` with two `{old, new}` edits |
   | 12 | picking the right tool | `search`, not `read_file` |

   It recommends `native` when at most one turn failed and at least half came back as
   structured calls; otherwise `prompted` (tools described in the prompt, calls written as
   text). The result is cached per server and model in `~/.crit/cache/doctor.json`;
   a profile with `"tool_mode": "auto"` (the `generic` profile, Gemma 4, Granite, Phi-4)
   uses it. The command exits 1 when the server is unreachable or more than half the turns
   fail.

Options: `--model NAME` (check another served model), `--no-probe` (server checks only),
`--write-profile` (save the findings as `~/.crit/profiles/<model>.json`; `--force` replaces
an existing one), `--json`, `--config FILE`.

## Model profiles

Built-in profiles ship in `critique_bot/profiles/*.json`. Each sets only what its model card
states; anything not verified is left out, and the server's default applies.

| Profile | Matches (examples) | Tool mode | Notes |
|---|---|---|---|
| `qwen3` | `qwen3:8b`, `Qwen/Qwen3-32B` | native | Hermes calls; thinking not sent back |
| `qwen3-coder` | `qwen3-coder:30b`, `Qwen3-Coder-480B-A35B` | native | XML calls (`qwen3_coder` parser) |
| `qwen3.5` | `qwen3.5-*` | native | XML calls; coding sampling from the card |
| `qwen3.6`, `qwen3.6-27b` | `qwen/qwen3.6-27b`, `Qwen3.6-27B-Q4_K_M.gguf` | native | `preserve_thinking`; 27B: 32K effective context (plan default) |
| `qwen3.8` | `qwen3.7*`, `qwen3.8*` | native | no sampling set yet |
| `deepseek-v3` | `deepseek-v3*`, `deepseek-chat` | native | temperature 0.3 |
| `deepseek-r1-distill` | `deepseek-r1:14b`, `DeepSeek-R1-Distill-*` | prompted | reasoning never sent back |
| `glm-4.5`, `glm-4.7` | `glm-4.5-air`, `z-ai/glm-4.6`, `glm-4.7` | native | 4.7 keeps thinking across turns |
| `devstral`, `mistral-small` | `devstral-small-2507`, `mistral-small-3.2-*`, `codestral*` | native | `mistral` parser |
| `gpt-oss` | `openai/gpt-oss-20b` | native | harmony format via the server |
| `llama3`, `llama4` | `llama-3.3-70b`, `llama-4-scout` | native | Llama 3: no parallel calls |
| `gemma3`, `gemma4` | `gemma3:12b`, `gemma-4-*` | prompted / auto | Gemma 3 has no tool tokens |
| `kimi-k2`, `minimax-m2` | `moonshotai/kimi-k2-instruct`, `minimax-m2` | native | MiniMax keeps thinking |
| `granite`, `phi4` | `granite-3.3-8b`, `phi-4-mini` | auto | the probe decides |
| `generic` | anything else | auto | no sampling sent |

**Resolution:** `local.profile` in config.json when set, else the profile whose `match`
glob fits the model name best (the most specific glob wins; your profiles win a tie), else
`generic`. Names are matched lowercased, as given and by their last path part, with a
`.gguf` suffix dropped, so OpenRouter (`qwen/qwen3.6-27b`), Hugging Face (`Qwen/Qwen3.6-27B`),
Ollama (`qwen3.6:27b`) and GGUF file names all work.

### Fields

| Field | Meaning |
|---|---|
| `id` | the profile's name (`local.profile` uses it) |
| `extends` | another profile id to copy fields from (size overrides: `qwen3.6-27b` extends `qwen3.6`) |
| `match` | name globs (`*`, `?`), lowercase |
| `tool_mode` | `native` (the server's tool calling), `prompted` (the engine's text format; no tools in the request), `auto` (native unless the doctor's cached probe says prompted) |
| `sampling` | `temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repetition_penalty`, sent with every request (`local.temperature` wins) |
| `preserve_thinking` | `true`: keep the model's reasoning on assistant messages in the history (`reasoning_content`); `false` (default): never send it back |
| `edit_format`, `edit_fallback` | `str_replace`, `whole_file` or `apply_patch` (used by the Stage 2 edit tools) |
| `effective_context` | tokens the model works well within; crit sizes chats to the smaller of this and the server's window (an explicit `local.num_ctx` wins) |
| `tool_output_cap_tokens`, `shell_timeout_s` | per-model tool limits (used by the Stage 2 tool layer) |
| `max_tokens` | `max_tokens` when `local.max_tokens` is 0 |
| `parallel_tool_calls` | sent with native tools when set; `false` for models that over-call |
| `parsers` | per server, the options for the launch line, e.g. `{"vllm": {"tool_call_parser": "qwen3_coder"}}` |
| `source`, `notes` | the model card the values come from, and anything to know |

### Your own profiles

Put JSON files in `~/.crit/profiles/` (one profile, a list, or `{"profiles": [...]}`).
A profile with a built-in's `id` changes only the fields it sets:

```json
{ "id": "qwen3.6", "sampling": { "temperature": 0.3 }, "max_tokens": 8192 }
```

A new `id` adds a profile; `extends` reuses a built-in:

```json
{
  "id": "my-qwen-finetune",
  "extends": "qwen3.6",
  "match": ["acme-coder-*"],
  "tool_mode": "prompted",
  "notes": "our fine-tune; native calls broke in the doctor probe"
}
```

A file with an error is skipped with a warning. `crit doctor model --write-profile` writes a
starting file for a model no profile matches.

## Server presets

Presets are in `critique_bot/profiles/servers/*.json`; `crit doctor model` prints the
matching launch line. Flags change between releases: each preset says so, and none is
marked as tested with a specific version yet (`tested_version: null`). Check `--help` of the
version you run.

### llama.cpp (`llama-server`)

```bash
llama-server -m MODEL.gguf --jinja -c 32768 -ngl 99 --cache-type-k q8_0 --cache-type-v q8_0 --port 8080
```

- `--jinja` is required for native tool calls; add `--chat-template-file` when the GGUF's
  template has no tools section.
- KV cache f16 or q8_0, not q4_0. A quantized V cache needs flash attention (`-fa`; newer
  builds take `-fa on|off|auto`).
- crit keeps one slot per conversation; keep `-np` at or above the number of helper chats.
- Speculation: a draft model with `-md draft.gguf --draft-max/--draft-min` (older builds:
  `--draft`). N-gram / lookup options exist in some builds under changing names.

### vLLM

```bash
vllm serve MODEL --max-model-len 32768 --enable-auto-tool-choice \
  --tool-call-parser <family parser> --reasoning-parser <family parser> --port 8000
```

The parsers come from the profile (`qwen3_coder`/`qwen3`, `glm45`, `mistral`, `openai`,
`llama3_json`, `kimi_k2`, `minimax_m2`, ...). Prefix caching is automatic in the V1 engine.
Speculation: `--speculative-config '{"method": "ngram", "num_speculative_tokens": 5, "prompt_lookup_max": 4}'`
on recent versions; older ones used `--speculative-model '[ngram]'` with separate options.

### SGLang

```bash
python -m sglang.launch_server --model-path MODEL --context-length 32768 \
  --tool-call-parser <family> --reasoning-parser <family> --port 30000
```

Radix cache is on by default. Speculation: `--speculative-algorithm` and its draft options,
which differ by version; `/get_server_info` (and so the doctor) shows when it is on.

### Ollama

```bash
OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
```

crit uses Ollama's OpenAI-compatible endpoint: `"url": "http://127.0.0.1:11434"`
(`local.api` stays `openai`). The model's template must include tools
(`ollama show --template MODEL`). Ollama's default context is far too small for an agent:
set `OLLAMA_CONTEXT_LENGTH` (older releases: `num_ctx` in a Modelfile). No speculation.

### LM Studio

```bash
lms server start --port 1234 && lms load MODEL --context-length 32768
```

Use a model LM Studio marks as tool-use capable. `/api/v0/models` reports the context length.

### Hugging Face TGI

```bash
text-generation-launcher --model-id MODEL --max-total-tokens 32768 --port 8080
```

Tool calls use TGI's guided generation; flags and supported models differ by version. When
the probe fails, use a profile with `"tool_mode": "prompted"`.

### MLX-LM (macOS)

```bash
mlx_lm.server --model MODEL --port 8080
```

Tool-call support depends on the mlx-lm version and the model's template; prompted mode is
often the reliable choice. The doctor reports it as a generic server.

### OpenRouter (hosted)

Hosted gateways send your prompts to a third party: use them for synthetic or public code
only, never private code. Three things are required:

```bash
export CRIT_API_KEY=sk-or-...          # or: crit config --api-key
crit config --backend local --local-url https://openrouter.ai/api --model qwen/qwen3.6-27b
crit config --allow-host openrouter.ai
```

and `max_tokens` in config.json, because without it OpenRouter reserves credit for the
model's whole output window and refuses with HTTP 402 on a small balance:

```json
{
  "backend": "local",
  "url": "https://openrouter.ai/api",
  "model": "qwen/qwen3.6-27b",
  "allowed_hosts": ["openrouter.ai"],
  "local": { "max_tokens": 8192 }
}
```

The profile is found by the part after the slash (`qwen3.6-27b`). Then run
`crit doctor model`: the provider behind the model decides how well tool calls work.

## Speculative decoding benchmark

Whole-file rewrites mostly copy text from the prompt, so n-gram speculation or a draft model
can speed them up; a preset keeps speculation only when rewrites get at least 1.5× faster
and normal turns slow down by at most 5%. Measure it with the server restarted between runs:

```bash
python scripts/eval/spec_bench.py run --url http://127.0.0.1:8080 --model M --label off --out off.json
python scripts/eval/spec_bench.py run --url http://127.0.0.1:8080 --model M --label on --out on.json
python scripts/eval/spec_bench.py compare off.json on.json   # exit 0 = keep it
```

It reports the median decode speed (tokens per second after the first token) for a rewrite
of a 600-line file (`--file` for your own) and for a short question. Record results in
`plan/results/01/speculation.md`.
