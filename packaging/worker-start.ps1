# Start the critique-bot worker on a Windows GitLab runner.
#
# Install under Program Files (only administrators can write there), e.g.
#   C:\Program Files\critique-bot\critique-bot.exe
# and keep the browser profile and queue under the worker account's own
# profile or a folder this script locks down. Do not install into a folder that
# ordinary users (or CI jobs) can write to: anyone who can replace the exe or
# config.json there runs code as the worker, with its signed-in browser.
#
# Run it at logon or as a scheduled task under a dedicated worker account
# (not the account the GitLab runner service uses):
#
#   schtasks /Create /TN critique-bot-worker /SC ONLOGON /RL LIMITED `
#     /TR "powershell.exe -File ""C:\Program Files\critique-bot\worker-start.ps1"""
#
# This script does NOT change PATH. Put the full exe path in .gitlab-ci.yml
# (CRITIQUE_BIN), or have an administrator add the Program Files folder to
# the system PATH. A user-writable folder early in PATH lets anyone who can
# write there shadow other programs.
#
# Before starting, the install folder gets an explicit ACL: inheritance off,
# full control for SYSTEM, Administrators, and the account running this script
# only. If the GitLab runner uses another account, name it in $RunnerAccount:
# it gets read/execute on the install folder (to run `submit`) and modify on
# the queue folder only. The browser profile is made private to the worker
# account by critique-bot itself. Set $SkipAcl = $true if your administrators
# manage the ACLs themselves.

$InstallDir = Join-Path $env:ProgramFiles "critique-bot"
$QueueDir = Join-Path $InstallDir ".critique-queue"   # must match queue_dir in config.json
$RunnerAccount = ""                                   # e.g. "CORP\svc-gitlab-runner"
$SkipAcl = $false
$Config = Join-Path $InstallDir "config.json"
$Exe = @(
    (Join-Path $InstallDir "critique-bot.exe"),
    (Join-Path $InstallDir ".venv\Scripts\critique-bot.exe")
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $Exe) {
    throw "critique-bot.exe not found under $InstallDir (zip exe or .venv\Scripts). Set `$InstallDir."
}
if (-not (Test-Path $Config)) {
    throw "config.json not found at $Config"
}

function Set-InstallAcl {
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # Well-known SIDs: S-1-5-18 = SYSTEM, S-1-5-32-544 = Administrators.
    # Grant first, then drop inheritance, so a failed grant never locks everyone out.
    & icacls.exe $InstallDir /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" "*${me}:(OI)(CI)F" | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    & icacls.exe $InstallDir /inheritance:r | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    if ($RunnerAccount) {
        & icacls.exe $InstallDir /grant "${RunnerAccount}:(OI)(CI)RX" | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
        New-Item -ItemType Directory -Force -Path $QueueDir | Out-Null
        & icacls.exe $QueueDir /grant "${RunnerAccount}:(OI)(CI)M" | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    return $true
}

function Test-BroadWrite {
    # True when Everyone, Users, or Authenticated Users can change the install folder.
    $broad = @("S-1-1-0", "S-1-5-32-545", "S-1-5-11")
    $writeBits = [System.Security.AccessControl.FileSystemRights]"Write, Modify, FullControl, CreateFiles, AppendData, Delete, ChangePermissions, TakeOwnership"
    foreach ($rule in (Get-Acl $InstallDir).Access) {
        if ($rule.AccessControlType -ne "Allow") { continue }
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { continue }
        if ($broad -contains $sid -and ($rule.FileSystemRights -band $writeBits)) { return $true }
    }
    return $false
}

if (-not $SkipAcl) {
    if (-not (Set-InstallAcl)) {
        # A non-admin worker account cannot change ACLs under Program Files. That is
        # fine as long as ordinary users cannot write there; refuse to start otherwise.
        if (Test-BroadWrite) {
            throw "$InstallDir is writable by ordinary users and its ACL could not be set. Run this script once as an administrator, or install under Program Files."
        }
        Write-Warning "could not set the ACL on $InstallDir (run once as administrator to apply it); ordinary users cannot write there, continuing"
    }
}

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Set-Location $InstallDir
& $Exe worker --config $Config --logs
