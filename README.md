# FTD Mode Switcher

Web UI on a Raspberry Pi that controls the `D:\ftd\products\Load_testing`
content on every Windows client. Two axes of control:

- **mode**     — `training` or `development` (a git branch)
- **versions** — per mode, which version of each software component is active

On any change, every Windows client assembles the selected component versions
and mirrors them into `D:\ftd\products\Load_testing` within ~15 seconds.

## Architecture

```
  Raspberry Pi                              Windows client(s)
  ┌──────────────────────────┐              ┌────────────────────────────────┐
  │ FastAPI :8089            │◄ poll /15s ──│ sync-agent.ps1 (boot task)     │
  │  state.json:             │              │  read mode + selected versions │
  │   current_mode           │◄ git pull ───│  checkout branch               │
  │   selections per mode    │              │  stage <comp>/<ver>/* files    │
  │ git bare repo            │              │  robocopy /MIR stage →         │
  │  training, development   │              │  D:\ftd\products\Load_testing  │
  │  with <component>/<ver>  │              │                                │
  └──────────────────────────┘              └────────────────────────────────┘
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
3. Switch mode or change a version selection in the UI.
4. Within 15s, the Windows log shows the deploy and `D:\ftd\products\Load_testing`
   reflects the union of the selected versions.

Quick API checks:
```bash
curl http://<rpi-ip>:8089/api/state         # active mode + resolved versions
curl http://<rpi-ip>:8089/api/components    # available components/versions per branch
```

## Populating real content

Layout convention per branch: a top-level folder is a **component**; each
subfolder is a **version** of that component. Loose files at branch root are
ignored by the deploy.

```
powerSwitchUI/
  1.0.0/        # files that ARE the load for this component+version
  2.0.0/
otherTool/      # add more components the same way
  0.9.0/
```

Push as many versions as you like — they don't deploy unless you select them
in the UI. Multiple components are deployed as a **union** (all selected
versions, flattened together into `Load_testing`).

```bash
git clone ftd@<rpi-ip>:/home/ftd/repos/ftd.git ftd-content
cd ftd-content

git checkout development
mkdir -p powerSwitchUI/2.0.0
cp -r /path/to/powerSwitchUI-2.0.0/* powerSwitchUI/2.0.0/
git add -A && git commit -m "powerSwitchUI 2.0.0" && git push

git checkout training
# stable versions for training go here, same layout
```

Then in the web UI, pick the active version per component for each mode. The
selection is saved to `state.json`; clients pick it up on their next poll.

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
