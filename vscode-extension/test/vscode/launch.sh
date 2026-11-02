#!/usr/bin/env bash
# Usage: test/vscode/launch.sh PYTHON
# Runs test/vscode/run.js in a throwaway VS Code profile under xvfb.
set -euo pipefail
here="$(cd "$(dirname "$0")/../.." && pwd)"
python="$1"
work="$(mktemp -d)"
port=$((13000 + RANDOM % 2000))
"$python" "$here/test/fake_model.py" "$port" >/dev/null 2>&1 &
server=$!
trap 'kill $server 2>/dev/null; rm -rf "$work"' EXIT
mkdir -p "$work/ws/.vscode"
echo one > "$work/ws/note.txt"
git -C "$work/ws" init -q
echo "{\"backend\":\"local\",\"url\":\"http://127.0.0.1:$port\",\"model\":\"fake\",\"local\":{\"api\":\"ollama\"}}" > "$work/ws/config.json"
echo '{"crit.permissionMode":"auto","crit.configPath":"config.json"}' > "$work/ws/.vscode/settings.json"
xvfb-run -a "${VSCODE_BIN:-/usr/share/code/code}" --no-sandbox --disable-gpu --disable-extensions --skip-welcome --skip-release-notes \
  --user-data-dir "$work/user" --extensions-dir "$work/exts" \
  --extensionDevelopmentPath="$here" --extensionTestsPath="$here/test/vscode/run.js" "$work/ws"
