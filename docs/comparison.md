# crit vs opencode

This page compares crit with [opencode](https://opencode.ai) on the same tasks, the same
model and the same model server. It reports only what `crit eval` measured; every number
links to the run that produced it, and anyone can rerun it.

!!! warning "No results yet"
    The tables below are templates. Numbers are filled in only from a published run that
    follows the method on this page. Until then every cell says TODO.

## Method

- **Suite:** the crit eval suite in `tests/eval_tasks/` at commit `TODO` (TODO tasks:
  bug fixes, features, refactors, builds, git, log analysis, safety). `crit eval audit`
  passes on that commit: every check fails on the untouched task and passes with its
  reference.
- **Model and server, held fixed:** TODO model (TODO quantization) on TODO server, version
  TODO, launched with `TODO launch line`, context TODO tokens, on TODO GPU. Both agents use
  the same server process and the model's recommended sampling.
- **Agents:** crit TODO (commit TODO) with its default settings and the model's profile;
  opencode **TODO version** (record the exact `opencode --version`), configured in
  `opencode.json` to use the same OpenAI-compatible endpoint and model, with its
  recommended settings for that model.
- **Trials:** TODO runs per task per agent (at least 3), each in a fresh temporary git
  workspace with a fresh `CRIT_HOME`; hidden checks are copied in only after the agent
  finishes.
- **Statistics:** pass@1 is the mean over trials; pass^k counts a task only when all k
  trials pass. Differences come with a paired bootstrap 90% interval over the tasks both
  runs share (`crit eval compare`); an interval that includes 0 is reported as no
  difference.

### Reproduce it

```bash
# crit
crit eval --config TODO.json --runs 3 --out .crit/eval/crit --note "TODO server, quant, flags"
# opencode, same server and model
crit eval --runner opencode --model TODO --runs 3 --out .crit/eval/opencode --note "TODO"
# the comparison
crit eval compare .crit/eval/opencode .crit/eval/crit
```

<!-- TODO: check the flags above against `crit eval --help` on the commit used, and link the
     raw results (results.jsonl, summary.md, meta.json) of both runs. -->

## Results

| Metric | opencode TODO | crit TODO | Difference (90% interval) |
| --- | --- | --- | --- |
| pass@1 | TODO | TODO | TODO |
| pass^3 | TODO | TODO | TODO |
| Malformed tool calls per turn | TODO | TODO | TODO |
| Tool errors per turn | TODO | TODO | TODO |
| Edit success rate | TODO | TODO | TODO |
| Median seconds per task | TODO | TODO | TODO |
| Completion tokens per solved task | TODO | TODO | TODO |
| Prompt cache hit ratio | TODO | TODO | TODO |

Per category (pass@1):

| Category | Tasks | opencode | crit |
| --- | --- | --- | --- |
| TODO | TODO | TODO | TODO |

## What this does not show

- One model and one server. Other models, quantizations and servers can rank differently.
- The suite is crit's own. It was written to cover what crit's users do (including Android
  and AOSP work), so it is not a neutral benchmark; the tasks and graders are public so
  that anyone can check them.
- Features that one agent has and the other lacks (crit's sandbox, opencode's UI and
  integrations, for example) are not scored here.
