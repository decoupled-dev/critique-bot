# SPDX-License-Identifier: Apache-2.0
"""Telemetry, usage analytics, and update checks: off for crit and everything it starts.

The only data that may leave the PC is the conversation with the configured chat
(see docs/security.md). Libraries and tools often report usage on their own, so
crit turns that off in two places:

* ``apply_to_process()`` runs first thing in every crit command. It sets the
  switches below in crit's own environment, so Playwright's driver, the browser it
  starts, and every program crit runs inherit them.
* The agent's commands get ``TELEMETRY_OFF_ENV`` again on top of their environment
  (agent_shell), in case a caller passed an environment of its own.

The values are forced, not defaults: a ``DO_NOT_TRACK=0`` left in the user's shell
does not turn reporting back on for crit.

crit's own Python dependencies (playwright, rich, prompt_toolkit and what they pull
in) send nothing: they contain no reporting code (checked for each pinned version;
tests/test_privacy.py keeps the dependency list from growing unreviewed).
"""

from __future__ import annotations

import os
from typing import Mapping

TELEMETRY_OFF_ENV: Mapping[str, str] = {
    # The cross-tool convention (consoledonottrack.com), honored by many CLIs.
    "DO_NOT_TRACK": "1",
    # Python tooling and ML libraries
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "ANONYMIZED_TELEMETRY": "False",  # chroma and libraries built on it
    "SCARF_ANALYTICS": "false",  # Scarf install analytics, in npm and Python packages
    "WANDB_MODE": "disabled",
    "GRADIO_ANALYTICS_ENABLED": "False",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
    "SEMGREP_SEND_METRICS": "off",
    # JavaScript toolchains
    "npm_config_audit": "false",  # npm install would upload the dependency list
    "npm_config_fund": "false",
    "npm_config_update_notifier": "false",
    "NO_UPDATE_NOTIFIER": "1",
    "DISABLE_OPENCOLLECTIVE": "1",
    "ADBLOCK": "1",  # opencollective-postinstall and similar install-time pings
    "YARN_ENABLE_TELEMETRY": "0",
    "NEXT_TELEMETRY_DISABLED": "1",
    "NUXT_TELEMETRY_DISABLED": "1",
    "GATSBY_TELEMETRY_DISABLED": "1",
    "ASTRO_TELEMETRY_DISABLED": "1",
    "STORYBOOK_DISABLE_TELEMETRY": "1",
    "TURBO_TELEMETRY_DISABLED": "1",
    "NG_CLI_ANALYTICS": "false",
    "EXPO_NO_TELEMETRY": "1",
    "CYPRESS_CRASH_REPORTS": "0",
    "DENO_NO_UPDATE_CHECK": "1",
    "WRANGLER_SEND_METRICS": "false",
    "VERCEL_TELEMETRY_DISABLED": "1",
    # .NET and PowerShell
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "POWERSHELL_TELEMETRY_OPTOUT": "1",
    "POWERSHELL_UPDATECHECK": "Off",
    # Cloud and infrastructure CLIs (blocked as network commands, but builds may call them)
    "AZURE_CORE_COLLECT_TELEMETRY": "0",
    "FUNCTIONS_CORE_TOOLS_TELEMETRY_OPTOUT": "1",
    "SAM_CLI_TELEMETRY": "0",
    "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING": "true",
    "CHECKPOINT_DISABLE": "1",  # HashiCorp: terraform, packer, vagrant
    "PULUMI_SKIP_UPDATE_CHECK": "true",
    "VCPKG_DISABLE_METRICS": "1",
    "HOMEBREW_NO_ANALYTICS": "1",
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "FLUTTER_SUPPRESS_ANALYTICS": "true",
}


def apply_to_process(environ: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """Force every switch in ``TELEMETRY_OFF_ENV`` into ``environ`` (crit's own environment by default)."""
    target = os.environ if environ is None else environ
    for name, value in TELEMETRY_OFF_ENV.items():
        target[name] = value
