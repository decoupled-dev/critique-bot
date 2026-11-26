# Install crit for this Windows user (no administrator rights needed).
#
#   irm https://github.com/decoupled-dev/critique-bot/releases/latest/download/install.ps1 | iex
#
# Downloads the latest release for Windows x64, checks it against the release's
# SHA256SUMS.txt, and runs "crit.exe install", which puts crit in
# %USERPROFILE%\.crit (app\<version> and bin; $env:CRIT_HOME moves it) and adds its bin
# folder to your PATH. Settings are asked the first time crit needs them. Set $env:CRIT_VERSION = "1.0.7" for a given version (1.0.5 or later).
# Set $env:CRIT_CONFIG_FILE = "\\server\share\crit.json" to also set the config file
# (url, page elements, ...), so crit asks nothing on first run.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$repo = "decoupled-dev/critique-bot"

if ([Environment]::Is64BitOperatingSystem -eq $false) { throw "crit needs 64-bit Windows 10 or 11." }

if ($env:CRIT_VERSION) {
    $tag = "v" + $env:CRIT_VERSION.TrimStart("v")
} else {
    $tag = (Invoke-RestMethod "https://api.github.com/repos/$repo/releases/latest" -Headers @{ "User-Agent" = "crit-installer" }).tag_name
}
$version = $tag.TrimStart("v")
$zipName = "crit-$version-windows-x64.zip"
$base = "https://github.com/$repo/releases/download/$tag"

$work = Join-Path ([IO.Path]::GetTempPath()) ("crit-install-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    Write-Host "Downloading crit $version ..."
    Invoke-WebRequest "$base/$zipName" -OutFile (Join-Path $work $zipName) -UseBasicParsing
    Invoke-WebRequest "$base/SHA256SUMS.txt" -OutFile (Join-Path $work "SHA256SUMS.txt") -UseBasicParsing

    $expected = (Get-Content (Join-Path $work "SHA256SUMS.txt") | Where-Object { $_ -match "\s\*?$([regex]::Escape($zipName))$" } | Select-Object -First 1)
    if (-not $expected) { throw "$zipName is not listed in SHA256SUMS.txt" }
    $expected = ($expected -split "\s+")[0].ToLower()
    $actual = (Get-FileHash (Join-Path $work $zipName) -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) { throw "Checksum mismatch for $zipName (expected $expected, got $actual). Nothing was installed." }

    Expand-Archive (Join-Path $work $zipName) -DestinationPath $work -Force
    $exe = Get-ChildItem $work -Recurse -Filter "crit.exe" | Select-Object -First 1
    if (-not $exe) { throw "crit.exe not found in $zipName" }
    if ($env:CRIT_CONFIG_FILE) {
        & $exe.FullName install --config-file $env:CRIT_CONFIG_FILE
    } else {
        & $exe.FullName install
    }
    if ($LASTEXITCODE -ne 0) { throw "crit install failed (exit $LASTEXITCODE)" }
    # This window does not see the new PATH yet; make crit work in it right away.
    $critHome = if ($env:CRIT_HOME) { $env:CRIT_HOME } else { Join-Path $env:USERPROFILE ".crit" }
    $bin = Join-Path $critHome "bin"
    if (($env:Path -split ";") -notcontains $bin) { $env:Path = "$env:Path;$bin" }
} finally {
    Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
}
