"""LLM client for code review and general prompts.

Default backend drives a web chat UI in headless Edge. Set ``backend`` to
``ollama``, ``openai``, or ``openai-compatible`` to call an HTTP API instead.
Default mode is a specialized code reviewer; ``--mode general`` sends an
arbitrary prompt and optional files; ``--mode chat`` is an interactive
terminal session. On a GitLab runner, ``worker`` keeps the backend open and
``submit`` enqueues reviews from CI jobs.
"""

__version__ = "0.1.0"
