"""Browser chat client for AAOS system-app review and general prompts.

Drives a web chat UI in headless Edge. Default mode is a specialized AAOS
privileged-app reviewer; ``--mode general`` sends an arbitrary prompt and
optional files; ``--mode chat`` is an interactive terminal session. On a
GitLab runner, ``worker`` keeps Edge open and ``submit`` enqueues reviews
from CI jobs.
"""

__version__ = "0.1.0"
