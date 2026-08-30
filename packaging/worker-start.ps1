# Start the critique-bot worker on a Windows GitLab runner.
# Edit the paths, then run at login or register as a scheduled task.
#
#   schtasks /Create /TN critique-bot-worker /SC ONLOGON /RL LIMITED `
#     /TR "powershell.exe -File C:\critique-bot\worker-start.ps1"

$InstallDir = "C:\critique-bot"
$Config = Join-Path $InstallDir "config.json"
$Exe = Join-Path $InstallDir "critique-bot.exe"

Set-Location $InstallDir
& $Exe worker --config $Config --logs
