# SPDX-License-Identifier: Apache-2.0
"""crit: a local-first coding agent and code reviewer.

``crit`` (the agent, the default) works in a local checkout with tools, through a model
served on this PC or the network (llama.cpp, vLLM, Ollama, LM Studio, ...) with native
tool calls, under an OS sandbox. ``crit review`` reviews a patch; ``crit eval`` measures
the harness. The optional web chat (``critique_bot.webchat``) drives a chat page in Edge
instead. See docs/architecture.md for the layers.
"""

__version__ = "2.0.0b1"
