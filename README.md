# FTD Mode Switcher

Web UI on a Raspberry Pi that flips all Windows clients between `training` and
`development` mode. Each mode is a git branch; on switch, every Windows client
mirrors the branch contents to `D:\ftd\products\Load_testing` within ~15 seconds.

## Architecture

```
  Raspberry Pi                              Windows client(s)
  ┌────────────────────┐                    ┌──────────────────────────┐
  │ FastAPI :8089      │◄─── poll /15s ─────│ sync-agent.ps1 (boot task)│
  │ git bare repo      │◄─── git pull ──────│ C:\git\ftd → mirror →    │
  │  training          │                    │ D:\ftd\products\         │
  │  development       │                    │   Load_testing           │
  └────────────────────┘                    └──────────────────────────┘
```

## One-line install on the Raspberry Pi

```bash
curl -fsSL https://raw.githubusercontent.com/edkeysender/ftd-load-change/main/install.sh | sudo bash
```

That's it. The installer will:
- Install git, Python 3, FastAPI
- Create a dedicated `ftd` user
- Initialize a bare repo with `training` and `development` branches (seeded with
  `MODE_TRAINING.txt` / `MODE_DEVELOPMENT.txt` so you can verify the switch works)
- Install a systemd service that runs at boot
- Print the URL of the web UI

## Updating after you push changes

```bash
sudo /opt/ftd-load-change/update.sh
```

## SSH keys for Windows clients

Each Windows machine needs SSH access to the `ftd` user on the RPi (read access is
enough for the agent — it only pulls):

On the RPi:
```bash
sudo mkdir -p /home/ftd/.ssh
sudo chmod 700 /home/ftd/.ssh
sudo touch /home/ftd/.ssh/authorized_keys
sudo chmod 600 /home/ftd/.ssh/authorized_keys
sudo chown -R ftd:ftd /home/ftd/.ssh
```

On each Windows machine (PowerShell):
```powershell
ssh-keygen -t ed25519     # if you don't have a key yet
Get-Content $HOME\.ssh\id_ed25519.pub    # copy this
```

Paste the public key into `/home/ftd/.ssh/authorized_keys` on the RPi.

Test from Windows:
```powershell
ssh ftd@<rpi-ip> "echo ok"
```

## Set up each Windows machine

1. Install Git for Windows: https://git-scm.com/download/win
2. Download `windows/sync-agent.ps1` and `windows/install-task.ps1` from this repo
3. Edit the two lines at the top of `sync-agent.ps1`:
   ```powershell
   [string]$RpiHost = "192.168.1.50"
   [string]$RepoUrl = "ftd@192.168.1.50:/home/ftd/repos/ftd.git"
   ```
4. Test manually (PowerShell as Administrator):
   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File .\sync-agent.ps1
   ```
   Check `D:\ftd\products\Load_testing` — `MODE_TRAINING.txt` should appear.
5. Install as boot-time scheduled task:
   ```powershell
   powershell.exe -ExecutionPolicy Bypass -File .\install-task.ps1
   ```

## Verify

1. Open `http://<rpi-ip>:8089` in a browser.
2. On Windows: `Get-Content C:\git\ftd-sync.log -Wait -Tail 20`
3. Click "Switch to DEVELOPMENT" in the web UI.
4. Within 15s, the Windows log shows the deploy and `D:\ftd\products\Load_testing`
   contains `MODE_DEVELOPMENT.txt` instead of `MODE_TRAINING.txt`.

## Populating real content

```bash
git clone ftd@<rpi-ip>:/home/ftd/repos/ftd.git ftd-content
cd ftd-content
git checkout training
rm MODE_TRAINING.txt
# ... add real files ...
git add . && git commit -m "Real training content" && git push

git checkout development
rm MODE_DEVELOPMENT.txt
# ... add real files ...
git add . && git commit -m "Real development content" && git push
```

After pushing, the next mode switch triggers a deploy of the new content.

## Repo layout

```
install.sh              # one-line bootstrap (clones + runs setup)
update.sh               # re-pull and re-run setup after pushing changes
rpi/
  setup.sh              # main RPi installer
  app.py                # FastAPI app
windows/
  sync-agent.ps1        # polls RPi, deploys on mode change
  install-task.ps1      # registers boot-time scheduled task
```

## Troubleshooting

**Service status**: `sudo systemctl status ftd-mode.service`
**Live logs**: `sudo journalctl -u ftd-mode.service -f`
**Windows agent log**: `C:\git\ftd-sync.log`
**Test API**: `curl http://<rpi-ip>:8089/api/state`

## Uninstall

```bash
# RPi
sudo systemctl stop ftd-mode.service
sudo systemctl disable ftd-mode.service
sudo rm /etc/systemd/system/ftd-mode.service
sudo userdel -r ftd
sudo rm -rf /opt/ftd-load-change
```

```powershell
# Windows
Unregister-ScheduledTask -TaskName "FTD-Mode-Sync" -Confirm:$false
Remove-Item C:\git -Recurse -Force
```
