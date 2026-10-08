<#
.SYNOPSIS
Install critique-bot into a virtual environment and put crit, bot-agent, and
critique-bot on the user PATH (Windows).

.DESCRIPTION
Automates the manual steps in docs/bot-agent.md:
  1. finds Python 3.10 or newer (py -3, then python / python3; the Microsoft
     Store python.exe stub is skipped),
  2. creates the virtual environment and runs pip install -e,
  3. checks for Microsoft Edge or Google Chrome,
  4. creates config.json from config.example.json if missing and rewrites a
     relative user_data_dir to a full path next to the config file,
  5. writes crit.cmd, bot-agent.cmd, and critique-bot.cmd into BinDir,
  6. puts BinDir first on the user PATH (and in this session),
  7. runs crit --help through the new command.
Safe to re-run: every step checks what is already in place.

If running scripts is blocked on this PC, start it with:
  powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1

.PARAMETER Venv
Virtual environment to create/use. Default: <repo>\.venv

.PARAMETER Config
Config file the commands use. Default: <repo>\config.json

.PARAMETER BinDir
Folder for crit.cmd, bot-agent.cmd, critique-bot.cmd.
Default: %LOCALAPPDATA%\critique-bot\bin

.PARAMETER Proxy
Proxy for every pip install, for example
http://username:password@10.1.2.3:8080.
uv installs receive the same URL through HTTP_PROXY and HTTPS_PROXY.

.PARAMETER WithIndex
Also install the [index] extra (tree-sitter parsers).

.PARAMETER NoPath
Do not change the user PATH.

.PARAMETER SkipBrowserCheck
Do not look for Microsoft Edge / Google Chrome.

.EXAMPLE
.\scripts\setup-crit.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1 -WithIndex

.EXAMPLE
.\scripts\setup-crit.ps1 -Proxy "http://username:password@10.1.2.3:8080"
#>
[CmdletBinding()]
param(
    [string]$Venv = "",
    [string]$Config = "",
    [string]$BinDir = "",
    [string]$Proxy = "",
    [switch]$WithIndex,
    [switch]$NoPath,
    [switch]$SkipBrowserCheck
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$WrapperMarker = "rem critique-bot: written by scripts\setup-crit.ps1"

function Write-Step([string]$Text) { Write-Host "==> $Text" }
function Write-Info([string]$Text) { Write-Host "    $Text" }
function Write-Warn([string]$Text) { Write-Warning $Text }
function Stop-Setup([string]$Text) { throw "ERROR: $Text" }

# Run a native program without PowerShell 5.1 turning its stderr into
# terminating errors. Returns the exit code; output goes to the host.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [switch]$Quiet)
    $ErrorActionPreference = "Continue"
    if ($Quiet) {
        & $Exe @Arguments *> $null
    } else {
        & $Exe @Arguments | Out-Host
    }
    return $LASTEXITCODE
}

# Run a native program and capture stdout (stderr discarded).
function Get-NativeOutput {
    param([string]$Exe, [string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $out = & $Exe @Arguments 2> $null
    if ($LASTEXITCODE -ne 0) { return $null }
    return (@($out) -join "`n").Trim()
}

function Get-FullPath([string]$Path) {
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Test-StoreStub([string]$Path) {
    # %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe is an App Execution
    # Alias. Without a Store Python installed it only opens the Store.
    if ($Path -notmatch '\\WindowsApps\\') { return $false }
    $apps = Split-Path -Parent $Path
    $real = @(Get-ChildItem -Path $apps -Directory -Filter "PythonSoftwareFoundation.Python.3*" -ErrorAction SilentlyContinue)
    return ($real.Count -eq 0)
}

$VersionCheck = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
$ExeQuery = "import sys; print(sys.executable) if sys.version_info >= (3, 10) else sys.exit(1)"

function Find-Python {
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        foreach ($tag in @("-3", "-3.13", "-3.12", "-3.11", "-3.10")) {
            $exe = Get-NativeOutput $py.Path @($tag, "-c", $ExeQuery)
            if ($exe -and (Test-Path -LiteralPath $exe)) { return $exe }
        }
    }
    foreach ($name in @("python", "python3")) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $path = $cmd.Path
            if (Test-StoreStub $path) {
                Write-Info "skipping the Microsoft Store stub $path"
                continue
            }
            $exe = Get-NativeOutput $path @("-c", $ExeQuery)
            if ($exe -and (Test-Path -LiteralPath $exe)) { return $exe }
        }
    }
    return $null
}

# New user PATH value with $Dir first, de-duplicated, other entries kept in order.
function Get-MergedPath([string]$Current, [string]$Dir) {
    $want = $Dir.TrimEnd([char]92)
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add($Dir)
    $seen = @{}
    $seen[$want.ToLowerInvariant()] = $true
    if ($Current) {
        foreach ($entry in ($Current -split ";")) {
            if (-not $entry.Trim()) { continue }
            $norm = $entry.Trim().TrimEnd([char]92)
            $expanded = [Environment]::ExpandEnvironmentVariables($norm).TrimEnd([char]92)
            $key = $norm.ToLowerInvariant()
            if ($seen.ContainsKey($key) -or $seen.ContainsKey($expanded.ToLowerInvariant())) { continue }
            $seen[$key] = $true
            $parts.Add($entry)
        }
    }
    return ($parts -join ";")
}

# Hide userinfo in http://user:password@host:port when printing.
function Get-RedactedProxy([string]$Url) {
    if ($Url -match '^(?<scheme>[a-z][a-z0-9+.-]*://)[^/@]+@(?<rest>.+)$') {
        return ($Matches.scheme + '***@' + $Matches.rest)
    }
    return $Url
}

# pip arguments with --proxy URL appended when a proxy was given.
function Get-PipInstallArgs([string]$ProxyUrl, [string[]]$Arguments) {
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($item in @($Arguments)) {
        if ($null -ne $item -and "$item" -ne "") { $list.Add([string]$item) }
    }
    if ($ProxyUrl) {
        $list.Add("--proxy")
        $list.Add($ProxyUrl)
    }
    return ,$list.ToArray()
}

# Text of one .cmd wrapper (CRLF line endings).
function Get-WrapperText([string]$Exe, [string]$ConfigPath) {
    $exeQ = '"' + $Exe.Replace("%", "%%") + '"'
    if ($ConfigPath) {
        $line = $exeQ + ' --config "' + $ConfigPath.Replace("%", "%%") + '" %*'
    } else {
        $line = $exeQ + " %*"
    }
    return "@echo off`r`n$WrapperMarker`r`n$line`r`n"
}

function Write-Wrapper([string]$Target, [string]$Text) {
    $enc = [Text.Encoding]::ASCII
    if ($Text -match '[^\x00-\x7F]') {
        try {
            $enc = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
        } catch {
            Write-Warn "a path has non-ASCII characters; $Target may not work in cmd"
        }
    }
    if (Test-Path -LiteralPath $Target -PathType Leaf) {
        $old = [IO.File]::ReadAllText($Target, $enc)
        if ($old -eq $Text) {
            Write-Info "$Target is up to date"
            return
        }
        if ($old -notmatch [regex]::Escape($WrapperMarker)) {
            $backup = "$Target.bak"
            if (Test-Path -LiteralPath $backup) {
                $backup = "$Target.bak." + (Get-Date -Format "yyyyMMddHHmmss")
            }
            Move-Item -LiteralPath $Target -Destination $backup
            Write-Info "moved the existing $Target to $backup"
        }
    }
    [IO.File]::WriteAllText($Target, $Text, $enc)
    Write-Info "wrote $Target"
}

function Send-SettingChange {
    try {
        if (-not ("CritSetup.NativeMethods" -as [type])) {
            Add-Type -Namespace CritSetup -Name NativeMethods -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5 s
        [void][CritSetup.NativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, "Environment", 2, 5000, [ref]$result)
    } catch {
        Write-Info "could not notify other programs of the PATH change (new windows still see it)"
    }
}

$ConfigScript = @'
import json
import os
import sys

path = sys.argv[1]
with open(path, encoding="utf-8-sig") as fh:
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
'@

function Invoke-Setup {
    if ($env:OS -ne "Windows_NT") {
        Stop-Setup "this script is for Windows; on Linux and macOS run scripts/setup-crit.sh"
    }

    $repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    if (-not (Test-Path -LiteralPath (Join-Path $repo "pyproject.toml"))) {
        Stop-Setup "$repo does not look like the critique-bot checkout (no pyproject.toml)"
    }
    $venvDir = $Venv
    if (-not $venvDir) { $venvDir = Join-Path $repo ".venv" }
    $configPath = $Config
    if (-not $configPath) { $configPath = Join-Path $repo "config.json" }
    $binPath = $BinDir
    if (-not $binPath) { $binPath = Join-Path $env:LOCALAPPDATA "critique-bot\bin" }
    $venvDir = Get-FullPath $venvDir
    $configPath = Get-FullPath $configPath
    $binPath = (Get-FullPath $binPath).TrimEnd([char]92)
    $scripts = Join-Path $venvDir "Scripts"
    $venvPython = Join-Path $scripts "python.exe"

    Write-Step "critique-bot checkout: $repo"
    Write-Info "venv:    $venvDir"
    Write-Info "config:  $configPath"
    Write-Info "bin dir: $binPath"
    if ($Proxy) {
        if ($Proxy -notmatch '^[a-z][a-z0-9+.-]*://') {
            Stop-Setup "-Proxy must be a URL such as http://username:password@10.1.2.3:8080"
        }
        foreach ($name in @("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")) {
            Set-Item -Path "Env:$name" -Value $Proxy
        }
        Write-Info ("proxy:   " + (Get-RedactedProxy $Proxy))
    }

    # 1. Python
    Write-Step "Looking for Python 3.10 or newer"
    $python = Find-Python
    if (-not $python -and (Test-Path -LiteralPath $venvPython)) {
        if ((Invoke-Native $venvPython @("-c", $VersionCheck) -Quiet) -eq 0) {
            $python = $venvPython
            Write-Info "no Python 3.10+ found; using the one in the existing venv"
        }
    }
    if (-not $python) {
        Write-Host "ERROR: Python 3.10 or newer was not found." -ForegroundColor Red
        Write-Host "  Install it with:  winget install --id Python.Python.3.12"
        Write-Host "  or from https://www.python.org/downloads/ (keep 'py launcher' ticked)."
        Write-Host "  The 'python' that opens the Microsoft Store is only a placeholder."
        Stop-Setup "Python 3.10+ is required"
    }
    $pyVer = Get-NativeOutput $python @("-c", "import platform; print(platform.python_version())")
    Write-Info "using $python ($pyVer)"

    # 2. venv + pip install -e
    if (($env:CRIT_SETUP_SKIP_INSTALL -eq "1") -and (Test-Path -LiteralPath $venvPython)) {
        Write-Step "Skipping venv creation and pip install (CRIT_SETUP_SKIP_INSTALL=1)"
    } else {
        if (Test-Path -LiteralPath $venvPython) {
            Write-Step "Using existing virtual environment $venvDir"
            if ((Invoke-Native $venvPython @("-c", $VersionCheck) -Quiet) -ne 0) {
                Stop-Setup "$venvDir uses Python older than 3.10; delete it and re-run"
            }
        } else {
            Write-Step "Creating virtual environment $venvDir"
            if ((Invoke-Native $python @("-m", "venv", $venvDir)) -ne 0) {
                Stop-Setup "could not create the virtual environment at $venvDir"
            }
        }
        $uv = $null
        if ((Invoke-Native $venvPython @("-m", "pip", "--version") -Quiet) -ne 0) {
            $ensured = ((Invoke-Native $venvPython @("-m", "ensurepip", "--upgrade") -Quiet) -eq 0) -and
                ((Invoke-Native $venvPython @("-m", "pip", "--version") -Quiet) -eq 0)
            if ($ensured) {
                Write-Info "installed pip into the venv (ensurepip)"
            } else {
                $uv = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if (-not $uv) { Stop-Setup "pip is missing in $venvDir; delete that folder and re-run" }
                Write-Info "this venv has no pip; using uv pip instead"
            }
        }
        $target = $repo
        if ($WithIndex) {
            $target = "$repo[index]"
            Write-Step "Installing critique-bot with the [index] extra (pip install -e)"
        } else {
            Write-Step "Installing critique-bot (pip install -e)"
        }
        if ($uv) {
            # uv reads HTTP_PROXY / HTTPS_PROXY, set above from -Proxy.
            $rc = Invoke-Native $uv.Path @("pip", "install", "--quiet", "--python", $venvPython, "-e", $target)
        } else {
            Write-Info "upgrading pip"
            $upgradeArgs = @("-m", "pip", "install", "--quiet", "--upgrade", "pip")
            if ($Proxy) { $upgradeArgs += @("--proxy", $Proxy) }
            if ((Invoke-Native $venvPython $upgradeArgs) -ne 0) {
                Write-Warn "pip upgrade failed; continuing with the installed pip"
            }
            $installArgs = @("-m", "pip", "install", "--quiet", "-e", $target)
            if ($Proxy) { $installArgs += @("--proxy", $Proxy) }
            $rc = Invoke-Native $venvPython $installArgs
        }
        if ($rc -ne 0) { Stop-Setup "pip install -e $target failed" }
    }
    foreach ($exe in @("crit", "bot-agent", "critique-bot")) {
        if (-not (Test-Path -LiteralPath (Join-Path $scripts "$exe.exe"))) {
            Stop-Setup "$scripts\$exe.exe is missing; the pip install did not finish"
        }
    }

    # 3. Browser
    if ($SkipBrowserCheck) {
        Write-Step "Skipping browser check (-SkipBrowserCheck)"
    } else {
        Write-Step "Checking for Microsoft Edge or Google Chrome"
        $found = $null
        $appPaths = @()
        foreach ($exe in @("msedge.exe", "chrome.exe")) {
            foreach ($root in @("HKCU:\SOFTWARE", "HKLM:\SOFTWARE", "HKLM:\SOFTWARE\WOW6432Node")) {
                $appPaths += "$root\Microsoft\Windows\CurrentVersion\App Paths\$exe"
            }
        }
        foreach ($key in $appPaths) {
            $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
            if ($item -and ($item.PSObject.Properties.Name -contains "(default)")) {
                $candidate = ([string]$item."(default)").Trim('"')
                if ($candidate -and (Test-Path -LiteralPath $candidate)) { $found = $candidate; break }
            }
        }
        if (-not $found) {
            $pf86 = ${env:ProgramFiles(x86)}
            $candidates = @()
            foreach ($base in @($pf86, $env:ProgramFiles, $env:LOCALAPPDATA)) {
                if (-not $base) { continue }
                $candidates += Join-Path $base "Microsoft\Edge\Application\msedge.exe"
                $candidates += Join-Path $base "Google\Chrome\Application\chrome.exe"
            }
            foreach ($candidate in $candidates) {
                if (Test-Path -LiteralPath $candidate) { $found = $candidate; break }
            }
        }
        if ($found) {
            Write-Info "found $found"
        } else {
            Write-Warn "neither Microsoft Edge nor Google Chrome was found."
            Write-Host "  Install Edge:   winget install --id Microsoft.Edge"
            Write-Host "  or Chrome:      winget install --id Google.Chrome"
        }
    }

    # 4. Config
    Write-Step "Preparing config $configPath"
    $configDir = Split-Path -Parent $configPath
    if (-not (Test-Path -LiteralPath $configDir)) { New-Item -ItemType Directory -Force -Path $configDir | Out-Null }
    if (Test-Path -LiteralPath $configPath) {
        Write-Info "keeping existing $configPath"
    } else {
        Copy-Item -LiteralPath (Join-Path $repo "config.example.json") -Destination $configPath
        Write-Info "created $configPath from config.example.json"
        Write-Info "set the chat URL and selectors next (critique-bot setup, below)"
    }
    $tmpScript = Join-Path ([IO.Path]::GetTempPath()) ("crit-setup-" + [guid]::NewGuid().ToString("N") + ".py")
    [IO.File]::WriteAllText($tmpScript, $ConfigScript, (New-Object Text.UTF8Encoding $false))
    try {
        if ((Invoke-Native $venvPython @($tmpScript, $configPath)) -ne 0) {
            Stop-Setup "could not update user_data_dir in $configPath"
        }
    } finally {
        Remove-Item -LiteralPath $tmpScript -ErrorAction SilentlyContinue
    }

    # 5. Wrappers
    Write-Step "Writing commands into $binPath"
    New-Item -ItemType Directory -Force -Path $binPath | Out-Null
    Write-Wrapper (Join-Path $binPath "crit.cmd") (Get-WrapperText (Join-Path $scripts "crit.exe") $configPath)
    Write-Wrapper (Join-Path $binPath "bot-agent.cmd") (Get-WrapperText (Join-Path $scripts "bot-agent.exe") $configPath)
    Write-Wrapper (Join-Path $binPath "critique-bot.cmd") (Get-WrapperText (Join-Path $scripts "critique-bot.exe") "")

    # 6. PATH
    $pathChanged = $false
    if ($NoPath) {
        Write-Step "Not changing PATH (-NoPath); add $binPath to PATH yourself"
    } else {
        Write-Step "Putting $binPath first on the user PATH"
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("Environment", $true)
        if (-not $key) { $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey("Environment") }
        try {
            $current = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
            if ($key.GetValueNames() -contains "Path") {
                $existingKind = $key.GetValueKind("Path")
                if ($existingKind -eq [Microsoft.Win32.RegistryValueKind]::String) { $kind = $existingKind }
            }
            $merged = Get-MergedPath $current $binPath
            if ($merged -eq $current) {
                Write-Info "user PATH already starts with $binPath"
            } else {
                $key.SetValue("Path", $merged, $kind)
                $pathChanged = $true
                Write-Info "user PATH updated"
                Send-SettingChange
            }
        } finally {
            $key.Close()
        }
    }
    # This session: put BinDir first so the check below and next commands use it.
    $env:Path = Get-MergedPath $env:Path $binPath

    # 7. Verify
    $critCmd = Join-Path $binPath "crit.cmd"
    Write-Step "Checking $critCmd --help"
    if ((Invoke-Native $critCmd @("--help") -Quiet) -ne 0) {
        Stop-Setup "$critCmd --help failed"
    }
    Write-Info "OK"

    # 8. Next steps
    Write-Step "Done. Next steps:"
    if ($pathChanged) {
        Write-Info "0. Open a new PowerShell window (this one already has the new PATH)"
    } elseif ($NoPath) {
        Write-Info "0. Put $binPath on PATH"
    }
    Write-Info "1. Pick the chat URL and selectors, and sign in:"
    Write-Info "     critique-bot setup --config `"$configPath`""
    Write-Info "2. Check a one-word reply:"
    Write-Info "     critique-bot --config `"$configPath`" --mode general --prompt `"Reply with exactly one word: PONG.`""
    Write-Info "3. Use crit in any project:"
    Write-Info "     cd your-project; crit"
}

# CRIT_SETUP_NO_MAIN=1 only defines the functions (used by the tests).
if ($env:CRIT_SETUP_NO_MAIN -ne "1") {
    try {
        Invoke-Setup
    } catch {
        Write-Host $_.Exception.Message -ForegroundColor Red
        exit 1
    }
}
