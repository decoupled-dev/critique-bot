#!/bin/sh
# Install crit for this Linux user (no root needed).
#
#   curl -fsSL https://github.com/decoupled-dev/critique-bot/releases/latest/download/install.sh | sh
#
# Downloads the latest release for this machine (x64 or arm64), checks it against the
# release's SHA256SUMS.txt, and runs "critique-bot install", which puts crit in
# ~/.crit (app/<version> and bin, added to PATH if needed; CRIT_HOME moves it). Settings
# are asked the first time crit needs them. CRIT_VERSION=1.0.1 picks a version.
# CRIT_CONFIG_FILE=/path/to/crit.json also sets the config file (url, page elements, ...),
# so crit asks nothing on first run.
set -eu

repo="decoupled-dev/critique-bot"

case "$(uname -s)" in
    Linux) ;;
    *) echo "This installer is for Linux; on Windows use install.ps1." >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch="x64" ;;
    aarch64 | arm64) arch="arm64" ;;
    *) echo "No crit build for $(uname -m)." >&2; exit 1 ;;
esac
for tool in curl unzip sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || { echo "Please install $tool first." >&2; exit 1; }
done

if [ -n "${CRIT_VERSION:-}" ]; then
    tag="v${CRIT_VERSION#v}"
else
    tag=$(curl -fsSL -H "User-Agent: crit-installer" "https://api.github.com/repos/$repo/releases/latest" \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$tag" ] || { echo "Could not find the latest release." >&2; exit 1; }
fi
version="${tag#v}"
zip="critique-bot-$version-linux-$arch.zip"
base="https://github.com/$repo/releases/download/$tag"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT INT TERM

echo "Downloading crit $version ($arch) ..."
curl -fsSL -o "$work/$zip" "$base/$zip"
curl -fsSL -o "$work/SHA256SUMS.txt" "$base/SHA256SUMS.txt"
(
    cd "$work"
    grep -E "[[:space:]]\*?$zip\$" SHA256SUMS.txt > expected.txt || { echo "$zip is not listed in SHA256SUMS.txt" >&2; exit 1; }
    sha256sum -c expected.txt >/dev/null || { echo "Checksum mismatch for $zip. Nothing was installed." >&2; exit 1; }
)
unzip -q "$work/$zip" -d "$work"
exe=$(find "$work" -type f -name critique-bot | head -n 1)
[ -n "$exe" ] || { echo "critique-bot not found in $zip" >&2; exit 1; }
chmod +x "$exe"
if [ -n "${CRIT_CONFIG_FILE:-}" ]; then
    "$exe" install --config-file "$CRIT_CONFIG_FILE"
else
    "$exe" install
fi
