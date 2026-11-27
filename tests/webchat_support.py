"""For tests that exercise the optional web chat (critique_bot.webchat)."""

from __future__ import annotations

import importlib.util
import unittest

HAVE_WEBCHAT = importlib.util.find_spec("critique_bot.webchat") is not None

#: Skip a test or class when the web chat package is not installed.
needs_webchat = unittest.skipUnless(HAVE_WEBCHAT, "the web chat (critique_bot.webchat) is not installed")
