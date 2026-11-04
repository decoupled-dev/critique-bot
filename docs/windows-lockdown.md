# Locking down a Windows PC for crit

Goal: **the only data that leaves this PC is the conversation with the chat URL.** crit's checks
([security.md](security.md)) catch the common ways data could leave, but they run inside the app.
A build, a test, or a Gradle plugin runs the repository's own code, which can connect anywhere.
Only Windows itself can enforce the rule, so the guarantee comes from step 6 (the firewall).

Do the steps in order. Steps 1 to 5 take about 15 minutes; step 6 needs an administrator, or your IT team.

---

## 1. Update crit

The fixes are on the `fix/review-findings-v2` branch (until it is merged into `main`).

```powershell
cd C:\path\to\critique-bot
git fetch origin
git checkout fix/review-findings-v2
git pull
powershell -ExecutionPolicy Bypass -File scripts\setup-crit.ps1
```

If this PC runs the zip build (`critique-bot.exe` under `C:\critique-bot`), replace it with a zip built from this branch.

Check: `crit --help` runs.

## 2. Check config.json

Open the `config.json` that crit uses (by default next to the repo, or the one `.bot\settings.json` names).

- `url`: your company chat address, starting with `https://`.
- The chat page runs as it does in a normal browser; crit does not filter its requests unless you set `"filter_chat_page": true`. If you do set it, list in `allowed_hosts` only hosts that belong to the chat service; crit prints any host it blocks.
- `cdp_url`: empty, or `http://127.0.0.1:<port>`. Never another machine.
- `user_data_dir`: a folder **outside** every project, for example `%LOCALAPPDATA%\critique-bot\edge-profile`. That folder holds the signed-in session cookies.

## 3. Make sure no config or settings file is committed

crit now refuses a `config.json` that git tracks in the project. It also ignores permission settings from a committed `.bot\settings.json`, because a cloned repository could otherwise choose where your code is sent.

In each project you use crit in:

```powershell
git ls-files config.json .bot/settings.json
```

If either file is listed, untrack it and ignore it:

```powershell
git rm --cached config.json .bot/settings.json
Add-Content .gitignore "config.json`n.bot/settings.json"
git commit -m "Stop tracking local crit settings"
```

## 4. Set safe defaults in .bot\settings.json

Edit `.bot\settings.json` in each project (it is your own file now, not committed):

```json
{
  "config": "C:\\path\\to\\config.json",
  "permissions": "edits",
  "offline": true
}
```

- `"permissions": "edits"` lets file edits run without asking, and asks before every command.
  `"ask"` is stricter: it asks for edits too. In `"auto"`, commands that are not read-only still ask.
- `"offline": true` makes package managers, the Gradle wrapper, and git work offline.
  **First run one normal build with offline off** (for example `.\gradlew build`), so the dependencies are in the local caches. After that, builds run offline.
- Leave out `web_fetch` and `network_commands`. They are off and blocked by default.

## 5. VS Code (if you use the extension)

crit's settings now count only in **User** settings. A repository's `.vscode\settings.json` cannot change them.

Ctrl+Shift+P → **Preferences: Open User Settings (JSON)**, then set what you need:

```json
"crit.configPath": "C:\\path\\to\\config.json",
"crit.permissionMode": "edits"
```

Also turn off VS Code's own network calls:

```json
"telemetry.telemetryLevel": "off",
"update.mode": "none",
"extensions.autoCheckUpdates": false,
"extensions.autoUpdate": false,
"workbench.enableExperiments": false,
"npm.fetchOnlinePackageInfo": false
```

## 6. Block every other connection with Windows Firewall (the guarantee)

Windows Defender Firewall allows by IP address and program, not by host name. Choose the case that fits this PC.

### Case A: a dedicated PC (a GitLab runner or crit worker that does nothing else)

Block all outbound traffic, then allow only what crit needs. Run **PowerShell as Administrator**.

1. Find the addresses to allow:

   ```powershell
   Resolve-DnsName chat.yourcompany.example      # the host in config.json "url"
   Resolve-DnsName gitlab.yourcompany.example    # only if this PC posts reviews to GitLab
   Resolve-DnsName nexus.yourcompany.example     # package mirror, only if builds run online
   Get-DnsClientServerAddress -AddressFamily IPv4 # your DNS servers
   ```

   If the chat is behind a CDN whose addresses keep changing, use Case B instead.

2. Add the allow rules. Replace the example addresses with yours:

   ```powershell
   New-NetFirewallRule -DisplayName "crit allow chat"   -Direction Outbound -Action Allow -Protocol TCP -RemotePort 443 -RemoteAddress 10.20.30.40
   New-NetFirewallRule -DisplayName "crit allow gitlab" -Direction Outbound -Action Allow -Protocol TCP -RemotePort 443 -RemoteAddress 10.20.30.50
   New-NetFirewallRule -DisplayName "crit allow dns"    -Direction Outbound -Action Allow -Protocol UDP -RemotePort 53  -RemoteAddress 10.0.0.53
   New-NetFirewallRule -DisplayName "crit allow dns tcp" -Direction Outbound -Action Allow -Protocol TCP -RemotePort 53 -RemoteAddress 10.0.0.53
   ```

   Loopback (`127.0.0.1`) is not filtered by Windows Firewall, so crit's local debugging port and setup page keep working.

3. Log blocked attempts, then block everything else:

   ```powershell
   Set-NetFirewallProfile -Profile Domain,Private,Public -LogBlocked True -LogMaxSizeKilobytes 16384 `
     -LogFileName "%windir%\system32\LogFiles\Firewall\pfirewall.log"
   Set-NetFirewallProfile -Profile Domain,Private,Public -DefaultOutboundAction Block
   ```

   Windows Update, Edge updates, and Microsoft Store stop working on this PC. Allow their addresses, or undo for a while, when you patch the PC.

4. To undo:

   ```powershell
   Set-NetFirewallProfile -Profile Domain,Private,Public -DefaultOutboundAction Allow
   Get-NetFirewallRule -DisplayName "crit allow*" | Remove-NetFirewallRule
   ```

**DNS is still a channel.** A lookup of `<data>.attacker.example` reaches the attacker's DNS server through your company's DNS server. crit blocks the lookup commands it can see, but a build could still do it. To close it fully, ask IT to make the DNS server this PC uses answer only internal names, or route the PC through a proxy (Case B).

### Case B: your everyday PC, or a chat behind a CDN

Blocking all outbound traffic would break normal work, and a host-name allowlist needs a proxy. Ask your IT or security team for an **outbound allowlist for this PC (or for your account)** on the company proxy or firewall. Send them this:

> Please allow outbound HTTPS from \<PC name / account\> only to:
> - the chat service: \<host from config.json "url"\> and \<each host in "allowed_hosts"\>
> - GitLab: \<gitlab host\> (only if this PC posts review comments)
> - the package mirror: \<Nexus / Artifactory host\> (only if builds run online)
>
> Block everything else, including direct DNS-over-HTTPS and UDP 443 (QUIC).
> Internal DNS should not resolve external names for this PC.

Until that is in place, steps 1 to 5 apply and crit's checks catch the obvious cases. There is no hard guarantee.

## 7. Edge policies (turn off Edge's own background services)

These are machine-wide and also apply to your everyday Edge. Run **PowerShell as Administrator**:

```powershell
$k = "HKLM:\SOFTWARE\Policies\Microsoft\Edge"
New-Item $k -Force | Out-Null
Set-ItemProperty $k SmartScreenEnabled 0 -Type DWord
Set-ItemProperty $k DiagnosticData 0 -Type DWord
Set-ItemProperty $k DnsOverHttpsMode "off" -Type String
Set-ItemProperty $k SyncDisabled 1 -Type DWord
Set-ItemProperty $k NetworkPredictionOptions 2 -Type DWord
New-Item "$k\ExtensionInstallBlocklist" -Force | Out-Null
Set-ItemProperty "$k\ExtensionInstallBlocklist" 1 "*" -Type String
```

Check: open `edge://policy` in Edge and confirm the values. If your company manages Edge through Group Policy or Intune, ask IT to set the same policies there.

## 8. Lock the worker folder (GitLab runner PCs only)

A folder created directly under `C:\` is writable by every signed-in user. The queue holds full prompts with source code, and the worker runs `critique-bot.exe` from that folder.

```powershell
icacls C:\critique-bot /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F"
```

If gitlab-runner runs as a different account, add `"<that account>:(OI)(CI)M"` to the same command.

In GitLab:
- set `gitlab.base_url` in config.json to an `https://` address
- mark the `GITLAB_TOKEN` CI variable **Protected** and **Masked**
- limit the runner to this project and to protected branches, so an MR author cannot edit `.gitlab-ci.yml` to run code on it

## 9. Check that it works

Open a test project and start crit.

1. Ask: *"run curl https://example.com"*. crit refuses: "nothing may leave this machine except the chat".
2. Ask: *"show me the .env file"* (create a dummy `.env` first). crit asks before reading it.
3. While a task runs, list the open connections:

   ```powershell
   Get-NetTCPConnection -State Established |
     Where-Object RemoteAddress -notmatch '^(127\.|::1)' |
     Select-Object RemoteAddress, RemotePort, @{n='Process';e={(Get-Process -Id $_.OwningProcess).Name}}
   ```

   Only the chat server's address (from `msedge`) should appear. On Case A, anything else is blocked.
4. Case A only: after a build, look for blocked attempts:

   ```powershell
   Select-String DROP "$env:windir\system32\LogFiles\Firewall\pfirewall.log" | Select-Object -Last 30
   ```

   Each line names a destination that something on this PC tried to reach and the firewall stopped.

## What crit does not cover

Step 6 covers these, which is why it is the guarantee:

- The chat page runs unfiltered by default, so its own scripts decide which hosts they call. `"filter_chat_page": true` limits it, at page level.
- The desktop app's spellchecker downloads dictionaries from Google until it is turned off in a later change.
- The sign-in window runs unfiltered by design, so that sign-in works.
