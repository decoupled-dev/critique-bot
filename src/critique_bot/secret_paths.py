# SPDX-License-Identifier: Apache-2.0
"""Where credentials live in a home folder: one list, shared by the checks that need it.

The OS sandbox masks these paths (:mod:`critique_bot.sandbox`), so a sandboxed command
reads them as empty. The list is kept here, in the core layer with no imports from crit,
so redaction's secret-file check (core) can use the same names as the sandbox (tools).

Paths are home-relative (``~/...``); a folder covers everything below it.
"""

from __future__ import annotations

#: Credential folders and files in the home folder: keys, cloud and forge logins, package
#: registry tokens, password stores, signed-in browsers, shell histories.
HOME_CREDENTIALS: tuple[str, ...] = (
    # keys and agents
    "~/.ssh", "~/.gnupg", "~/.password-store", "~/.local/share/keyrings", "~/.android/adbkey",
    # cloud and infrastructure
    "~/.aws", "~/.azure", "~/.config/gcloud", "~/.kube", "~/.docker", "~/.boto", "~/.s3cfg", "~/.oci",
    "~/.config/doctl", "~/.config/hcloud", "~/.config/op", "~/.op", "~/.config/sops", "~/.config/rclone",
    "~/.databrickscfg", "~/.databricks", "~/.terraformrc", "~/.terraform.d", "~/.vault-token",
    "~/.config/helm/repositories.yaml", "~/.cache/huggingface/token", "~/.huggingface/token",
    # git and forges
    "~/.netrc", "~/.git-credentials", "~/.config/git/credentials", "~/.config/gh", "~/.config/glab-cli",
    "~/.config/hub",
    # package registries
    "~/.npmrc", "~/.yarnrc", "~/.yarnrc.yml", "~/.pypirc", "~/.cargo/credentials", "~/.cargo/credentials.toml",
    "~/.gem/credentials", "~/.config/composer/auth.json", "~/.composer/auth.json", "~/.m2/settings.xml",
    "~/.m2/settings-security.xml", "~/.nuget/NuGet/NuGet.Config", "~/.config/NuGet/NuGet.Config",
    # databases
    "~/.pgpass", "~/.my.cnf",
    # signed-in browsers
    "~/.mozilla", "~/.config/google-chrome", "~/.config/chromium", "~/.config/microsoft-edge",
    # shell and REPL histories (secrets typed on a command line)
    "~/.bash_history", "~/.zsh_history", "~/.python_history", "~/.psql_history", "~/.mysql_history",
    "~/.node_repl_history",
)
