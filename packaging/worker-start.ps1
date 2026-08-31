# Start the critique-bot worker on a Windows GitLab runner.
# Edit $InstallDir, then run at login or register as a scheduled task.
#
#   schtasks /Create /TN critique-bot-worker /SC ONLOGON /RL LIMITED `
#     /TR "powershell.exe -File C:\critique-bot\worker-start.ps1"
#
# This script also prepends the exe directory to the current user's PATH
# so GitLab jobs can run `critique-bot` without a CI/CD variable. Restart
# gitlab-runner once after the first run so the job process sees the new PATH.
# If the runner is a Windows service (Local System), add that directory to
# the *system* PATH instead, or put the full exe path in .gitlab-ci.yml.

$InstallDir = "C:\critique-bot"
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

$binDir = Split-Path $Exe
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
$parts = @($userPath -split ";" | Where-Object { $_ })
if ($parts -notcontains $binDir) {
    [Environment]::SetEnvironmentVariable("Path", ($binDir + ";" + $userPath).TrimEnd(";"), "User")
}
$env:Path = $binDir + ";" + $env:Path

Set-Location $InstallDir
& $Exe worker --config $Config --logs
