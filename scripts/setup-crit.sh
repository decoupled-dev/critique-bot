#!/usr/bin/env bash
# Install critique-bot into a virtual environment and put `crit`,
# `bot-agent`, and `critique-bot` on PATH (Linux and macOS).
#
# Usage: scripts/setup-crit.sh [--venv DIR] [--config FILE] [--bin-dir DIR]
#                              [--proxy URL] [--with-index] [--no-path]
#                              [--skip-browser-check] [--install-deps] [-h|--help]
#
# Safe to re-run: every step checks what is already in place.
set -euo pipefail

WRAPPER_MARKER="# critique-bot: written by scripts/setup-crit.sh"
RC_MARKER="# critique-bot: added by scripts/setup-crit.sh"

usage() {
    cat <<'EOF'
Usage: scripts/setup-crit.sh [options]

Installs critique-bot into a virtual environment, prepares config.json, and
writes `crit`, `bot-agent`, and `critique-bot` commands into a bin folder on
PATH. Safe to re-run.

Options:
  --venv DIR            Virtual environment to create/use (default: <repo>/.venv)
  --config FILE         Config file the commands use (default: <repo>/config.json)
  --bin-dir DIR         Where the commands are written (default: ~/.local/bin)
  --proxy URL           Proxy for every pip install, for example
                        http://username:password@10.1.2.3:8080
  --with-index          Also install the [index] extra (tree-sitter parsers)
  --no-path             Do not add the bin folder to PATH in your shell rc file
  --skip-browser-check  Do not look for Microsoft Edge / Google Chrome
  --install-deps        Linux: run `playwright install-deps` (may need sudo)
  -h, --help            Show this help
EOF
}

# Hide userinfo in http://user:password@host:port when printing.
redact_proxy() {
    printf '%s' "$1" | sed -E 's#//[^/@]*@#//***@#'
}

say() { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Absolute path without requiring the target to exist (its parent is created).
abs_path() {
    local p=$1 dir base
    case $p in
        \~) p=$HOME ;;
        \~/*) p=$HOME/${p#\~/} ;;
    esac
    case $p in
        /*) ;;
        *) p=$PWD/$p ;;
    esac
    dir=$(dirname -- "$p")
    base=$(basename -- "$p")
    mkdir -p -- "$dir"
    dir=$(cd -P -- "$dir" && pwd)
    if [ "$base" = "." ] || [ "$base" = "/" ]; then
        printf '%s\n' "$dir"
    elif [ "$base" = ".." ]; then
        dirname -- "$dir"
    elif [ "$dir" = "/" ]; then
        printf '/%s\n' "$base"
    else
        printf '%s/%s\n' "$dir" "$base"
    fi
}

# Quote a string for /bin/sh using single quotes.
sh_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# Escape a string for use inside double quotes in a shell rc file.
dq_escape() {
    printf '%s' "$1" | sed -e 's/[\\"$`]/\\&/g'
}

# --- repo location (resolve symlinks without readlink -f, for macOS) ---------
src=${BASH_SOURCE[0]}
while [ -L "$src" ]; do
    src_dir=$(cd -P -- "$(dirname -- "$src")" && pwd)
    src=$(readlink -- "$src")
    case $src in
        /*) ;;
        *) src=$src_dir/$src ;;
    esac
done
script_dir=$(cd -P -- "$(dirname -- "$src")" && pwd)
repo=$(dirname -- "$script_dir")

venv=$repo/.venv
config=$repo/config.json
bin_dir=$HOME/.local/bin
proxy=""
with_index=0
no_path=0
skip_browser=0
install_deps=0

need_arg() {
    [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value (see --help)"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --venv) need_arg "$@"; venv=$2; shift 2 ;;
        --venv=*) venv=${1#*=}; shift ;;
        --config) need_arg "$@"; config=$2; shift 2 ;;
        --config=*) config=${1#*=}; shift ;;
        --bin-dir) need_arg "$@"; bin_dir=$2; shift 2 ;;
        --bin-dir=*) bin_dir=${1#*=}; shift ;;
        --proxy) need_arg "$@"; proxy=$2; shift 2 ;;
        --proxy=*) proxy=${1#*=}; [ -n "$proxy" ] || die "--proxy needs a value (see --help)"; shift ;;
        --with-index) with_index=1; shift ;;
        --no-path) no_path=1; shift ;;
        --skip-browser-check) skip_browser=1; shift ;;
        --install-deps) install_deps=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

[ -f "$repo/pyproject.toml" ] || die "$repo does not look like the critique-bot checkout (no pyproject.toml)"

if [ -n "$proxy" ]; then
    case $proxy in
        [a-zA-Z]*://*) ;;
        *) die "--proxy must be a URL such as http://username:password@10.1.2.3:8080" ;;
    esac
    # pip install ... --proxy URL, and the same URL for uv via the environment.
    export http_proxy="$proxy" https_proxy="$proxy" all_proxy="$proxy"
    export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy" ALL_PROXY="$proxy"
fi

venv=$(abs_path "$venv")
config=$(abs_path "$config")
bin_dir=$(abs_path "$bin_dir")
os=$(uname -s)

say "critique-bot checkout: $repo"
info "venv:    $venv"
info "config:  $config"
info "bin dir: $bin_dir"
if [ -n "$proxy" ]; then
    info "proxy:   $(redact_proxy "$proxy")"
fi

# Every pip install gets --proxy when one was given. pip --version stays local.
run_pip() {
    if [ -n "$proxy" ]; then
        "$venv_python" -m pip "$@" --proxy "$proxy"
    else
        "$venv_python" -m pip "$@"
    fi
}

# --- a. Python >= 3.10 --------------------------------------------------------
say "Looking for Python 3.10 or newer"
python=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$cand" >/dev/null 2>&1 \
        && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        python=$(command -v "$cand")
        break
    fi
done
if [ -z "$python" ] && [ -x "$venv/bin/python" ] \
    && "$venv/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    python=$venv/bin/python
    info "no Python 3.10+ on PATH; using the one in the existing venv"
fi
if [ -z "$python" ]; then
    printf 'ERROR: Python 3.10 or newer was not found on PATH.\n' >&2
    printf '  Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip\n' >&2
    printf '                 (older Ubuntu: sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv)\n' >&2
    printf '  Fedora/RHEL:   sudo dnf install python3 python3-pip\n' >&2
    printf '  macOS:         brew install python@3.12\n' >&2
    exit 1
fi
info "using $python ($("$python" -c 'import platform; print(platform.python_version())'))"

# --- b. venv + pip install -e ---------------------------------------------------
venv_python=$venv/bin/python
apt_venv_hint() {
    local ver
    ver=$("$python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    if command -v apt-get >/dev/null 2>&1; then
        printf '  The venv/ensurepip module is missing. Install it with:\n' >&2
        printf '    sudo apt install python%s-venv\n' "$ver" >&2
    fi
}

if [ "${CRIT_SETUP_SKIP_INSTALL:-}" = "1" ] && [ -x "$venv_python" ]; then
    say "Skipping venv creation and pip install (CRIT_SETUP_SKIP_INSTALL=1)"
else
    if [ -x "$venv_python" ]; then
        say "Using existing virtual environment $venv"
        "$venv_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
            || die "$venv uses Python older than 3.10; delete it and re-run"
    else
        say "Creating virtual environment $venv"
        if ! "$python" -m venv "$venv"; then
            apt_venv_hint
            die "could not create the virtual environment at $venv"
        fi
    fi
    use_uv=0
    if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
        if "$venv_python" -m ensurepip --upgrade >/dev/null 2>&1 \
            && "$venv_python" -m pip --version >/dev/null 2>&1; then
            info "installed pip into the venv (ensurepip)"
        elif command -v uv >/dev/null 2>&1; then
            use_uv=1
            info "this venv has no pip; using uv pip instead"
        else
            apt_venv_hint
            die "pip is missing in $venv; delete that folder and re-run after fixing the above"
        fi
    fi
    if [ "$with_index" = 1 ]; then
        target="${repo}[index]"
        say "Installing critique-bot with the [index] extra (pip install -e)"
    else
        target=$repo
        say "Installing critique-bot (pip install -e)"
    fi
    if [ "$use_uv" = 1 ]; then
        uv pip install --quiet --python "$venv_python" -e "$target"
    else
        info "upgrading pip"
        run_pip install --quiet --upgrade pip \
            || warn "pip upgrade failed; continuing with the installed pip"
        run_pip install --quiet -e "$target"
    fi
fi
for exe in crit bot-agent critique-bot; do
    [ -x "$venv/bin/$exe" ] || die "$venv/bin/$exe is missing; the pip install did not finish"
done

# --- c. Browser ------------------------------------------------------------------
if [ "$skip_browser" = 1 ]; then
    say "Skipping browser check (--skip-browser-check)"
else
    say "Checking for Microsoft Edge or Google Chrome"
    browser=""
    if [ "$os" = "Darwin" ]; then
        for app in "Microsoft Edge.app" "Google Chrome.app" "Chromium.app"; do
            for root in /Applications "$HOME/Applications"; do
                if [ -d "$root/$app" ]; then
                    browser=$root/$app
                    break 2
                fi
            done
        done
    else
        for b in microsoft-edge-stable microsoft-edge google-chrome google-chrome-stable chromium chromium-browser; do
            if command -v "$b" >/dev/null 2>&1; then
                browser=$(command -v "$b")
                break
            fi
        done
    fi
    if [ -n "$browser" ]; then
        info "found $browser"
    else
        warn "neither Microsoft Edge nor Google Chrome was found."
        if [ "$os" = "Darwin" ]; then
            printf '  Install one: brew install --cask microsoft-edge   (or google-chrome)\n' >&2
        else
            printf '  Install Edge: https://www.microsoft.com/edge/download  (package microsoft-edge-stable)\n' >&2
            printf '  or Chrome:    https://www.google.com/chrome/  (package google-chrome-stable)\n' >&2
        fi
    fi
fi
if [ "$install_deps" = 1 ]; then
    if [ "$os" = "Linux" ]; then
        say "Installing browser system libraries (playwright install-deps)"
        info "this uses sudo/apt and may ask for your password; if it fails, run as root:"
        info "  sudo \"$venv_python\" -m playwright install-deps"
        "$venv_python" -m playwright install-deps \
            || warn "playwright install-deps failed; run the command above with sudo"
    else
        info "--install-deps only applies to Linux; skipped"
    fi
fi

# --- d. Config -------------------------------------------------------------------
say "Preparing config $config"
if [ -e "$config" ]; then
    info "keeping existing $config"
else
    cp -- "$repo/config.example.json" "$config"
    info "created $config from config.example.json"
    info "set the chat URL and selectors next (critique-bot setup, below)"
fi
"$venv_python" - "$config" <<'PY'
import json
import os
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = json.load(fh)
if not isinstance(data, dict):
    sys.exit(f"ERROR: {path} is not a JSON object")
raw = data.get("user_data_dir")
value = raw.strip() if isinstance(raw, str) else ""
if value.lower() in ("system", "default"):
    print(f"    user_data_dir is {value!r} (dedicated Edge profile); left as is")
elif value and os.path.isabs(os.path.expanduser(value)):
    print(f"    user_data_dir is already absolute: {value}")
else:
    base = os.path.dirname(os.path.abspath(path))
    new = os.path.normpath(os.path.join(base, value or ".edge-profile"))
    data["user_data_dir"] = new
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    print(f"    user_data_dir: {raw!r} -> {new!r}")
PY

# --- e. Wrappers -------------------------------------------------------------------
say "Writing commands into $bin_dir"
mkdir -p -- "$bin_dir"
write_wrapper() {
    local name=$1 body=$2 target content backup
    target=$bin_dir/$name
    content=$(printf '#!/bin/sh\n%s\n%s\n' "$WRAPPER_MARKER" "$body")
    if [ -f "$target" ] && [ ! -L "$target" ] && [ "$(cat -- "$target")" = "$content" ]; then
        chmod 755 -- "$target"
        info "$target is up to date"
        return
    fi
    if [ -e "$target" ] || [ -L "$target" ]; then
        if [ -L "$target" ] || ! grep -qF -- "$WRAPPER_MARKER" "$target" 2>/dev/null; then
            backup=$target.bak
            if [ -e "$backup" ] || [ -L "$backup" ]; then
                backup=$target.bak.$(date +%Y%m%d%H%M%S)
            fi
            mv -- "$target" "$backup"
            info "moved the existing $target to $backup"
        fi
    fi
    printf '%s\n' "$content" >"$target.tmp.$$"
    chmod 755 -- "$target.tmp.$$"
    mv -f -- "$target.tmp.$$" "$target"
    info "wrote $target"
}
q_config=$(sh_quote "$config")
write_wrapper crit "exec $(sh_quote "$venv/bin/crit") --config $q_config \"\$@\""
write_wrapper bot-agent "exec $(sh_quote "$venv/bin/bot-agent") --config $q_config \"\$@\""
write_wrapper critique-bot "exec $(sh_quote "$venv/bin/critique-bot") \"\$@\""

# --- f. PATH ------------------------------------------------------------------------
on_path=0
case ":${PATH:-}:" in
    *":$bin_dir:"*) on_path=1 ;;
esac
rc_file=""
if [ "$on_path" = 1 ]; then
    say "$bin_dir is already on PATH"
elif [ "$no_path" = 1 ]; then
    say "Not changing PATH (--no-path); add $bin_dir to PATH yourself"
else
    case $(basename -- "${SHELL:-}") in
        zsh) rc_file=$HOME/.zshrc ;;
        bash) rc_file=$HOME/.bashrc ;;
        *) rc_file=$HOME/.profile ;;
    esac
    say "Adding $bin_dir to PATH in $rc_file"
    if [ -f "$rc_file" ] && grep -qF -- "$RC_MARKER" "$rc_file"; then
        info "$rc_file already has the PATH line"
    else
        {
            printf '\n%s\n' "$RC_MARKER"
            # shellcheck disable=SC2016  # $PATH is expanded by the rc file, not here
            printf 'export PATH="%s:$PATH"\n' "$(dq_escape "$bin_dir")"
        } >>"$rc_file"
        info "appended the PATH line to $rc_file"
    fi
    if [ "$os" = "Darwin" ] && [ "$rc_file" = "$HOME/.bashrc" ]; then
        info "macOS bash login shells read ~/.bash_profile; make sure it sources ~/.bashrc"
    fi
    info "open a new terminal, or run: source \"$rc_file\""
fi

# --- g. Verify ----------------------------------------------------------------------
say "Checking $bin_dir/crit --help"
if "$bin_dir/crit" --help >/dev/null; then
    info "OK"
else
    die "$bin_dir/crit --help failed"
fi

# --- h. Next steps ------------------------------------------------------------------
q_cfg_show=$(sh_quote "$config")
say "Done. Next steps:"
if [ "$on_path" = 0 ] && [ -n "$rc_file" ]; then
    info "0. Open a new terminal (or: source \"$rc_file\")"
elif [ "$on_path" = 0 ]; then
    info "0. Put $bin_dir on PATH"
fi
info "1. Pick the chat URL and selectors, and sign in:"
info "     critique-bot setup --config $q_cfg_show"
info "2. Check a one-word reply:"
info "     critique-bot --config $q_cfg_show --mode general --prompt \"Reply with exactly one word: PONG.\""
info "3. Use crit in any project:"
info "     cd your-project && crit"
